# Copyright 2022 InstaDeep Ltd. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import functools
from logging import Logger as SacredLogger
from typing import Any, Callable, Dict, Sequence, Tuple

import chex
import distrax
import flax.linen as nn
import hydra
import jax
import jax.numpy as jnp
import jumanji
import numpy as np
import optax
from colorama import Fore, Style
from flax.core.frozen_dict import FrozenDict
from flax.linen.initializers import constant, orthogonal
from jumanji.env import Environment
from jumanji.environments.routing.robot_warehouse.generator import RandomGenerator
from jumanji.wrappers import AutoResetWrapper
from omegaconf import DictConfig, OmegaConf
from optax._src.base import OptState
from sacred import run

from mava.evaluator import evaluator_setup
from mava.logger import logger_setup
from mava.types import (
    ExperimentOutput,
    HiddenStates,
    OptStates,
    Params,
    PPOTransition,
    RNNLearnerState,
)
from mava.utils.logger_tools import get_sacred_exp
from mava.utils.timing_utils import TimeIt
from mava.wrappers.jumanji import AgentIDWrapper, LogWrapper, RwareMultiAgentWrapper


class ScannedRNN(nn.Module):
    @functools.partial(
        nn.scan,
        variable_broadcast="params",
        in_axes=0,
        out_axes=0,
        split_rngs={"params": False},
    )
    @nn.compact
    def __call__(self, carry: chex.Array, x: chex.Array) -> Tuple[chex.Array, chex.Array]:
        """Applies the module."""
        rnn_state = carry
        ins, resets = x
        rnn_state = jnp.where(
            resets[:, np.newaxis],
            self.initialize_carry(ins.shape[0], ins.shape[1]),
            rnn_state,
        )
        new_rnn_state, y = nn.GRUCell()(rnn_state, ins)
        return new_rnn_state, y

    @staticmethod
    def initialize_carry(batch_size: int, hidden_size: int) -> chex.Array:
        """Initializes the carry state."""
        # Use a dummy key since the default state init fn is just zeros.
        return nn.GRUCell.initialize_carry(jax.random.PRNGKey(0), (batch_size,), hidden_size)


class Actor(nn.Module):
    """Actor Network."""

    action_dim: Sequence[int]

    @nn.compact
    def __call__(
        self,
        policy_hidden_state: chex.Array,
        observation_done: Tuple[chex.Array, chex.Array],
    ) -> Tuple[chex.Array, distrax.Categorical]:
        """Forward pass."""
        observation, done = observation_done

        policy_embedding = nn.Dense(
            128, kernel_init=orthogonal(np.sqrt(2)), bias_init=constant(0.0)
        )(observation.agents_view)
        policy_embedding = nn.relu(policy_embedding)

        policy_rnn_in = (policy_embedding, done)
        policy_hidden_state, policy_embedding = ScannedRNN()(policy_hidden_state, policy_rnn_in)

        actor_output = nn.Dense(128, kernel_init=orthogonal(2), bias_init=constant(0.0))(
            policy_embedding
        )
        actor_output = nn.relu(actor_output)
        actor_output = nn.Dense(
            self.action_dim, kernel_init=orthogonal(0.01), bias_init=constant(0.0)
        )(actor_output)

        masked_logits = jnp.where(
            observation.action_mask,
            actor_output,
            jnp.finfo(jnp.float32).min,
        )

        pi = distrax.Categorical(logits=masked_logits)

        return policy_hidden_state, pi


class Critic(nn.Module):
    """Critic Network."""

    @nn.compact
    def __call__(
        self,
        critic_hidden_state: Tuple[chex.Array, chex.Array],
        observation_done: Tuple[chex.Array, chex.Array],
    ) -> Tuple[chex.Array, chex.Array]:
        """Forward pass."""
        observation, done = observation_done

        critic_embedding = nn.Dense(
            128, kernel_init=orthogonal(np.sqrt(2)), bias_init=constant(0.0)
        )(observation.agents_view)
        critic_embedding = nn.relu(critic_embedding)

        critic_rnn_in = (critic_embedding, done)
        critic_hidden_state, critic_embedding = ScannedRNN()(critic_hidden_state, critic_rnn_in)

        critic = nn.Dense(128, kernel_init=orthogonal(2), bias_init=constant(0.0))(critic_embedding)
        critic = nn.relu(critic)
        critic = nn.Dense(1, kernel_init=orthogonal(1.0), bias_init=constant(0.0))(critic)

        return critic_hidden_state, jnp.squeeze(critic, axis=-1)


def get_learner_fn(
    env: jumanji.Environment,
    apply_fns: Tuple[Callable, Callable],
    update_fns: Tuple[Callable, Callable],
    config: Dict,
) -> Callable:
    """Get the learner function."""

    actor_apply_fn, critic_apply_fn = apply_fns
    actor_update_fn, critic_update_fn = update_fns

    def _update_step(learner_state: RNNLearnerState, _: Any) -> Tuple[RNNLearnerState, Tuple]:
        """A single update of the network.

        This function steps the environment and records the trajectory batch for
        training. It then calculates advantages and targets based on the recorded
        trajectory and updates the actor and critic networks based on the calculated
        losses.

        Args:
            learner_state (NamedTuple):
                - params (Params): The current model parameters.
                - opt_states (OptStates): The current optimizer states.
                - rng (PRNGKey): The random number generator state.
                - env_state (State): The environment state.
                - last_timestep (TimeStep): The last timestep in the current trajectory.
                - dones (bool): Whether the last timestep was a terminal state.
                - hstates (HiddenStates): The current hidden states of the RNN.
            _ (Any): The current metrics info.
        """

        def _env_step(
            learner_state: RNNLearnerState, _: Any
        ) -> Tuple[RNNLearnerState, PPOTransition]:
            """Step the environment."""
            (
                params,
                opt_states,
                rng,
                env_state,
                last_timestep,
                last_done,
                hstates,
            ) = learner_state

            rng, policy_rng = jax.random.split(rng)

            # Add a batch dimension to the observation.
            batched_observation = jax.tree_util.tree_map(
                lambda x: x[jnp.newaxis, :], last_timestep.observation
            )
            ac_in = (
                batched_observation,
                last_done[:, 0][jnp.newaxis, :],
            )

            # Run the network.
            policy_hidden_state, actor_policy = actor_apply_fn(
                params.actor_params, hstates.policy_hidden_state, ac_in
            )
            critic_hidden_state, value = critic_apply_fn(
                params.critic_params, hstates.critic_hidden_state, ac_in
            )

            # Sample action from the policy and squeeze out the batch dimension.
            action = actor_policy.sample(seed=policy_rng)
            log_prob = actor_policy.log_prob(action)
            value, action, log_prob = (
                value.squeeze(0),
                action.squeeze(0),
                log_prob.squeeze(0),
            )

            # Step the environment.
            env_state, timestep = jax.vmap(env.step, in_axes=(0, 0))(env_state, action)

            # log episode return and length
            done, reward = jax.tree_util.tree_map(
                lambda x: jnp.repeat(x, config["num_agents"]).reshape(config["num_envs"], -1),
                (timestep.last(), timestep.reward),
            )
            info = {
                "episode_return": env_state.episode_return_info,
                "episode_length": env_state.episode_length_info,
            }

            transition = PPOTransition(
                done, action, value, reward, log_prob, last_timestep.observation, info
            )
            hstates = HiddenStates(policy_hidden_state, critic_hidden_state)
            learner_state = RNNLearnerState(
                params, opt_states, rng, env_state, timestep, done, hstates
            )
            return learner_state, transition

        # INITIALISE RNN STATE
        initial_hstates = learner_state.hstates

        # STEP ENVIRONMENT FOR ROLLOUT LENGTH
        learner_state, traj_batch = jax.lax.scan(
            _env_step, learner_state, None, config["rollout_length"]
        )

        # CALCULATE ADVANTAGE
        (
            params,
            opt_states,
            rng,
            env_state,
            last_timestep,
            last_done,
            hstates,
        ) = learner_state

        # Add a batch dimension to the observation.
        batched_last_observation = jax.tree_util.tree_map(
            lambda x: x[jnp.newaxis, :], last_timestep.observation
        )
        ac_in = (
            batched_last_observation,
            last_done[:, 0][jnp.newaxis, :],
        )

        # Run the network.
        _, last_val = critic_apply_fn(params.critic_params, hstates.critic_hidden_state, ac_in)
        # Squeeze out the batch dimension and mask out the value of terminal states.
        last_val = last_val.squeeze(0)
        last_val = jnp.where(last_done, jnp.zeros_like(last_val), last_val)

        def _calculate_gae(
            traj_batch: PPOTransition, last_val: chex.Array
        ) -> Tuple[chex.Array, chex.Array]:
            """Calculate the GAE."""

            def _get_advantages(gae_and_next_value: Tuple, transition: PPOTransition) -> Tuple:
                """Calculate the GAE for a single transition."""
                gae, next_value = gae_and_next_value
                done, value, reward = (
                    transition.done,
                    transition.value,
                    transition.reward,
                )
                delta = reward + config["gamma"] * next_value * (1 - done) - value
                gae = delta + config["gamma"] * config["gae_lambda"] * (1 - done) * gae
                return (gae, value), gae

            _, advantages = jax.lax.scan(
                _get_advantages,
                (jnp.zeros_like(last_val), last_val),
                traj_batch,
                reverse=True,
                unroll=16,
            )
            return advantages, advantages + traj_batch.value

        advantages, targets = _calculate_gae(traj_batch, last_val)

        def _update_epoch(update_state: Tuple, _: Any) -> Tuple:
            """Update the network for a single epoch."""

            def _update_minibatch(train_state: Tuple, batch_info: Tuple) -> Tuple:
                """Update the network for a single minibatch."""

                params, opt_states = train_state
                (
                    init_policy_hstate,
                    init_critic_hstate,
                    traj_batch,
                    advantages,
                    targets,
                ) = batch_info

                def _actor_loss_fn(
                    actor_params: FrozenDict,
                    actor_opt_state: OptState,
                    traj_batch: PPOTransition,
                    gae: chex.Array,
                ) -> Tuple:
                    """Calculate the actor loss."""
                    # RERUN NETWORK

                    obs_and_done = (traj_batch.obs, traj_batch.done[:, :, 0])
                    _, actor_policy = actor_apply_fn(
                        actor_params, init_policy_hstate.squeeze(0), obs_and_done
                    )
                    log_prob = actor_policy.log_prob(traj_batch.action)

                    ratio = jnp.exp(log_prob - traj_batch.log_prob)
                    gae = (gae - gae.mean()) / (gae.std() + 1e-8)
                    loss_actor1 = ratio * gae
                    loss_actor2 = (
                        jnp.clip(
                            ratio,
                            1.0 - config["clip_eps"],
                            1.0 + config["clip_eps"],
                        )
                        * gae
                    )
                    loss_actor = -jnp.minimum(loss_actor1, loss_actor2)
                    loss_actor = loss_actor.mean()
                    entropy = actor_policy.entropy().mean()

                    total_loss = loss_actor - config["ent_coef"] * entropy
                    return total_loss, (loss_actor, entropy)

                def _critic_loss_fn(
                    critic_params: FrozenDict,
                    critic_opt_state: OptState,
                    traj_batch: PPOTransition,
                    targets: chex.Array,
                ) -> Tuple:
                    """Calculate the critic loss."""
                    # RERUN NETWORK
                    obs_and_done = (traj_batch.obs, traj_batch.done[:, :, 0])
                    _, value = critic_apply_fn(
                        critic_params, init_critic_hstate.squeeze(0), obs_and_done
                    )

                    # CALCULATE VALUE LOSS
                    value_pred_clipped = traj_batch.value + (value - traj_batch.value).clip(
                        -config["clip_eps"], config["clip_eps"]
                    )
                    value_losses = jnp.square(value - targets)
                    value_losses_clipped = jnp.square(value_pred_clipped - targets)
                    value_loss = 0.5 * jnp.maximum(value_losses, value_losses_clipped).mean()

                    total_loss = config["vf_coef"] * value_loss
                    return total_loss, (value_loss)

                # CALCULATE ACTOR LOSS
                actor_grad_fn = jax.value_and_grad(_actor_loss_fn, has_aux=True)
                actor_loss_info, actor_grads = actor_grad_fn(
                    params.actor_params, opt_states.actor_opt_state, traj_batch, advantages
                )

                # CALCULATE CRITIC LOSS
                critic_grad_fn = jax.value_and_grad(_critic_loss_fn, has_aux=True)
                critic_loss_info, critic_grads = critic_grad_fn(
                    params.critic_params, opt_states.critic_opt_state, traj_batch, targets
                )

                # Compute the parallel mean (pmean) over the batch.
                # This calculation is inspired by the Anakin architecture demo notebook.
                # available at https://tinyurl.com/26tdzs5x
                # This pmean could be a regular mean as the batch axis is on the same device.
                actor_grads, actor_loss_info = jax.lax.pmean(
                    (actor_grads, actor_loss_info), axis_name="batch"
                )
                # pmean over devices.
                actor_grads, actor_loss_info = jax.lax.pmean(
                    (actor_grads, actor_loss_info), axis_name="device"
                )

                critic_grads, critic_loss_info = jax.lax.pmean(
                    (critic_grads, critic_loss_info), axis_name="batch"
                )
                # pmean over devices.
                critic_grads, critic_loss_info = jax.lax.pmean(
                    (critic_grads, critic_loss_info), axis_name="device"
                )

                # UPDATE ACTOR PARAMS AND OPTIMISER STATE
                actor_updates, actor_new_opt_state = actor_update_fn(
                    actor_grads, opt_states.actor_opt_state
                )
                actor_new_params = optax.apply_updates(params.actor_params, actor_updates)

                # UPDATE CRITIC PARAMS AND OPTIMISER STATE
                critic_updates, critic_new_opt_state = critic_update_fn(
                    critic_grads, opt_states.critic_opt_state
                )
                critic_new_params = optax.apply_updates(params.critic_params, critic_updates)

                new_params = Params(actor_new_params, critic_new_params)
                new_opt_state = OptStates(actor_new_opt_state, critic_new_opt_state)

                # PACK LOSS INFO
                total_loss = actor_loss_info[0] + critic_loss_info[0]
                value_loss = critic_loss_info[1]
                actor_loss = actor_loss_info[1][0]
                entropy = actor_loss_info[1][1]
                loss_info = (
                    total_loss,
                    (value_loss, actor_loss, entropy),
                )

                return (new_params, new_opt_state), loss_info

            (
                params,
                opt_states,
                init_hstates,
                traj_batch,
                advantages,
                targets,
                rng,
            ) = update_state
            init_policy_hstate, init_critic_hstate = init_hstates
            rng, shuffle_rng = jax.random.split(rng)

            # SHUFFLE MINIBATCHES
            permutation = jax.random.permutation(shuffle_rng, config["num_envs"])
            batch = (init_policy_hstate, init_critic_hstate, traj_batch, advantages, targets)
            shuffled_batch = jax.tree_util.tree_map(
                lambda x: jnp.take(x, permutation, axis=1), batch
            )
            reshaped_batch = jax.tree_util.tree_map(
                lambda x: jnp.reshape(x, (x.shape[0], config["num_minibatches"], -1, *x.shape[2:])),
                shuffled_batch,
            )
            minibatches = jax.tree_util.tree_map(lambda x: jnp.swapaxes(x, 1, 0), reshaped_batch)

            # UPDATE MINIBATCHES
            (params, opt_states), loss_info = jax.lax.scan(
                _update_minibatch, (params, opt_states), minibatches
            )

            update_state = (
                params,
                opt_states,
                init_hstates,
                traj_batch,
                advantages,
                targets,
                rng,
            )
            return update_state, loss_info

        init_hstates = jax.tree_util.tree_map(lambda x: x[None, :], initial_hstates)
        update_state = (
            params,
            opt_states,
            init_hstates,
            traj_batch,
            advantages,
            targets,
            rng,
        )

        # UPDATE EPOCHS
        update_state, loss_info = jax.lax.scan(
            _update_epoch, update_state, None, config["ppo_epochs"]
        )

        params, opt_states, _, traj_batch, advantages, targets, rng = update_state
        learner_state = RNNLearnerState(
            params,
            opt_states,
            rng,
            env_state,
            last_timestep,
            last_done,
            hstates,
        )
        metric = traj_batch.info
        return learner_state, (metric, loss_info)

    def learner_fn(learner_state: RNNLearnerState) -> ExperimentOutput:
        """Learner function.

        This function represents the learner, it updates the network parameters
        by iteratively applying the `_update_step` function for a fixed number of
        updates. The `_update_step` function is vectorized over a batch of inputs.

        Args:
            learner_state (NamedTuple):
                - params (Params): The initial model parameters.
                - opt_states (OptStates): The initial optimizer states.
                - rng (chex.PRNGKey): The random number generator state.
                - env_state (LogEnvState): The environment state.
                - timesteps (TimeStep): The initial timestep in the initial trajectory.
                - dones (bool): Whether the initial timestep was a terminal state.
                - hstateS (HiddenStates): The initial hidden states of the RNN.
        """

        batched_update_step = jax.vmap(_update_step, in_axes=(0, None), axis_name="batch")

        learner_state, (metric, loss_info) = jax.lax.scan(
            batched_update_step, learner_state, None, config["num_updates_per_eval"]
        )
        total_loss, (value_loss, loss_actor, entropy) = loss_info
        return ExperimentOutput(
            learner_state=learner_state,
            episodes_info=metric,
            total_loss=total_loss,
            value_loss=value_loss,
            loss_actor=loss_actor,
            entropy=entropy,
        )

    return learner_fn


def learner_setup(
    env: Environment, rngs: chex.Array, config: Dict
) -> Tuple[Callable, Actor, RNNLearnerState]:
    """Initialise learner_fn, network, optimiser, environment and states."""
    # Get available TPU cores.
    n_devices = len(jax.devices())

    # Get number of actions and agents.
    num_actions = int(env.action_spec().num_values[0])
    num_agents = env.action_spec().shape[0]
    config["num_agents"] = num_agents

    # PRNG keys.
    rng, rng_p = rngs

    # Define network and optimisers.
    actor_network = Actor(num_actions)
    critic_network = Critic()
    actor_optim = optax.chain(
        optax.clip_by_global_norm(config["max_grad_norm"]),
        optax.adam(config["actor_lr"], eps=1e-5),
    )
    critic_optim = optax.chain(
        optax.clip_by_global_norm(config["max_grad_norm"]),
        optax.adam(config["critic_lr"], eps=1e-5),
    )

    # Initialise observation: Select only obs for a single agent.
    init_obs = env.observation_spec().generate_value()
    init_obs = jax.tree_util.tree_map(lambda x: x[0], init_obs)
    init_obs = jax.tree_util.tree_map(
        lambda x: jnp.repeat(x[jnp.newaxis, ...], config["num_envs"], axis=0),
        init_obs,
    )
    init_obs = jax.tree_util.tree_map(lambda x: x[None, ...], init_obs)
    init_done = jnp.zeros((1, config["num_envs"]), dtype=bool)
    init_x = (init_obs, init_done)

    # Initialise hidden states.
    init_policy_hstate = ScannedRNN.initialize_carry((config["num_envs"]), 128)
    init_critic_hstate = ScannedRNN.initialize_carry((config["num_envs"]), 128)

    # initialise params and optimiser state.
    actor_params = actor_network.init(rng_p, init_policy_hstate, init_x)
    actor_opt_state = actor_optim.init(actor_params)
    critic_params = critic_network.init(rng_p, init_critic_hstate, init_x)
    critic_opt_state = critic_optim.init(critic_params)

    # Vmap network apply function over number of agents.
    vmapped_actor_network_apply_fn = jax.vmap(
        actor_network.apply, in_axes=(None, 1, (2, None)), out_axes=(1, 2)
    )
    # Vmap network apply function over number of agents.
    vmapped_critic_network_apply_fn = jax.vmap(
        critic_network.apply, in_axes=(None, 1, (2, None)), out_axes=(1, 2)
    )

    # Get network apply functions and optimiser updates.
    apply_fns = (vmapped_actor_network_apply_fn, vmapped_critic_network_apply_fn)
    update_fns = (actor_optim.update, critic_optim.update)

    # Get batched iterated update and replicate it to pmap it over cores.
    learn = get_learner_fn(env, apply_fns, update_fns, config)
    learn = jax.pmap(learn, axis_name="device")

    # Broadcast params and optimiser state to cores and batch.
    broadcast = lambda x: jnp.broadcast_to(x, (n_devices, config["update_batch_size"]) + x.shape)
    actor_params = jax.tree_map(broadcast, actor_params)
    actor_opt_state = jax.tree_map(broadcast, actor_opt_state)
    critic_params = jax.tree_map(broadcast, critic_params)
    critic_opt_state = jax.tree_map(broadcast, critic_opt_state)

    # Duplicate the hidden state for each agent.
    init_policy_hstate = jnp.expand_dims(init_policy_hstate, axis=1)
    init_policy_hstate = jnp.tile(init_policy_hstate, (1, config["num_agents"], 1))
    policy_hstates = jax.tree_map(broadcast, init_policy_hstate)

    init_critic_hstate = jnp.expand_dims(init_critic_hstate, axis=1)
    init_critic_hstate = jnp.tile(init_critic_hstate, (1, config["num_agents"], 1))
    critic_hstates = jax.tree_map(broadcast, init_critic_hstate)

    # Initialise environment states and timesteps.
    rng, *env_rngs = jax.random.split(
        rng, n_devices * config["update_batch_size"] * config["num_envs"] + 1
    )
    env_states, timesteps = jax.vmap(env.reset, in_axes=(0))(
        jnp.stack(env_rngs),
    )

    # Split rngs for each core.
    rng, *step_rngs = jax.random.split(rng, n_devices * config["update_batch_size"] + 1)
    # Add dimension to pmap over.
    reshape_step_rngs = lambda x: x.reshape((n_devices, config["update_batch_size"]) + x.shape[1:])
    step_rngs = reshape_step_rngs(jnp.stack(step_rngs))
    reshape_states = lambda x: x.reshape(
        (n_devices, config["update_batch_size"], config["num_envs"]) + x.shape[1:]
    )
    env_states = jax.tree_util.tree_map(reshape_states, env_states)
    timesteps = jax.tree_util.tree_map(reshape_states, timesteps)

    # Initialise dones.
    dones = jnp.zeros(
        (
            n_devices,
            config["update_batch_size"],
            config["num_envs"],
            config["num_agents"],
        ),
        dtype=bool,
    )
    hstates = HiddenStates(policy_hstates, critic_hstates)
    params = Params(actor_params, critic_params)
    opt_states = OptStates(actor_opt_state, critic_opt_state)
    init_learner_state = RNNLearnerState(
        params=params,
        opt_states=opt_states,
        key=step_rngs,
        env_state=env_states,
        timestep=timesteps,
        dones=dones,
        hstates=hstates,
    )
    return learn, actor_network, init_learner_state


def run_experiment(_run: run.Run, _config: Dict, _log: SacredLogger) -> None:
    """Runs experiment."""
    # Logger setup
    config = copy.deepcopy(_config)
    log = logger_setup(_run, config, _log)

    # Create envs
    generator = RandomGenerator(**config["rware_scenario"]["task_config"])
    env = jumanji.make(config["env_name"], generator=generator)
    env = RwareMultiAgentWrapper(env)
    # Add agent id to observation.
    if config["add_agent_id"]:
        env = AgentIDWrapper(env)
    env = AutoResetWrapper(env)
    env = LogWrapper(env)
    eval_env = jumanji.make(config["env_name"], generator=generator)
    eval_env = RwareMultiAgentWrapper(eval_env)
    if config["add_agent_id"]:
        eval_env = AgentIDWrapper(eval_env)

    # PRNG keys.
    rng, rng_e, rng_p = jax.random.split(jax.random.PRNGKey(config["seed"]), num=3)

    # Setup learner.
    learn, actor_network, learner_state = learner_setup(env, (rng, rng_p), config)

    # Setup evaluator.
    evaluator, absolute_metric_evaluator, (trained_params, eval_rngs) = evaluator_setup(
        eval_env=eval_env,
        rng_e=rng_e,
        network=actor_network,
        params=learner_state.params.actor_params,
        config=config,
        use_recurrent_net=True,
        scanned_rnn=ScannedRNN,
    )

    # Calculate total timesteps.
    n_devices = len(jax.devices())
    config["num_updates_per_eval"] = config["num_updates"] // config["num_evaluation"]
    timesteps_per_training = (
        n_devices
        * config["num_updates_per_eval"]
        * config["rollout_length"]
        * config["update_batch_size"]
        * config["num_envs"]
    )

    # Run experiment for a total number of evaluations.
    max_episode_return = jnp.float32(0.0)
    best_params = None
    for i in range(config["num_evaluation"]):
        # Train.
        with TimeIt(
            tag=("COMPILATION" if i == 0 else "EXECUTION"),
            environment_steps=timesteps_per_training,
        ):
            learner_output = learn(learner_state)
            jax.block_until_ready(learner_output)

        # Log the results of the training.
        log(
            metrics=learner_output,
            t_env=timesteps_per_training * (i + 1),
            trainer_metric=True,
        )

        # Prepare for evaluation.
        trained_params = jax.tree_util.tree_map(
            lambda x: x[:, 0, ...], learner_output.learner_state.params.actor_params
        )
        rng_e, *eval_rngs = jax.random.split(rng_e, n_devices + 1)
        eval_rngs = jnp.stack(eval_rngs)
        eval_rngs = eval_rngs.reshape(n_devices, -1)

        # Evaluate.
        evaluator_output = evaluator(trained_params, eval_rngs)
        jax.block_until_ready(evaluator_output)

        # Log the results of the evaluation.
        episode_return = log(
            metrics=evaluator_output,
            t_env=timesteps_per_training * (i + 1),
        )
        if config["absolute_metric"] and max_episode_return <= episode_return:
            best_params = copy.deepcopy(trained_params)
            max_episode_return = episode_return

        # Update runner state to continue training.
        learner_state = learner_output.learner_state

    # Measure absolute metric.
    if config["absolute_metric"]:
        rng_e, *eval_rngs = jax.random.split(rng_e, n_devices + 1)
        eval_rngs = jnp.stack(eval_rngs)
        eval_rngs = eval_rngs.reshape(n_devices, -1)
        evaluator_output = absolute_metric_evaluator(best_params, eval_rngs)
        log(
            metrics=evaluator_output,
            t_env=timesteps_per_training * (i + 1),
            absolute_metric=True,
        )


@hydra.main(config_path="../configs", config_name="default.yaml", version_base="1.2")
def hydra_entry_point(cfg: DictConfig) -> None:
    """Experiment entry point."""
    # Convert config to python dict.
    cfg: Dict = OmegaConf.to_container(cfg, resolve=True)

    ex = get_sacred_exp(cfg, "rec_ippo_rware")

    # Run experiment.
    ex.main(run_experiment)
    ex.run(config_updates={})

    print(f"{Fore.CYAN}{Style.BRIGHT}Reccurent IPPO experiment completed{Style.RESET_ALL}")


if __name__ == "__main__":
    hydra_entry_point()
