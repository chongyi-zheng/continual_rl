from collections import Iterable
import torch

import utils
from agent.sac import TaskEmbeddingHyperNetActorSacMlpAgent


class SiTaskEmbeddingHyperNetActorSacMlpAgent(TaskEmbeddingHyperNetActorSacMlpAgent):
    def __init__(
            self,
            obs_shape,
            action_shape,
            action_range,
            device,
            actor_hidden_dim=400,
            critic_hidden_dim=256,
            discount=0.99,
            init_temperature=0.01,
            alpha_lr=1e-3,
            actor_lr=1e-3,
            actor_log_std_min=-10,
            actor_log_std_max=2,
            actor_update_freq=2,
            critic_lr=1e-3,
            critic_tau=0.005,
            critic_target_update_freq=2,
            batch_size=128,
            hypernet_hidden_dim=128,
            hypernet_task_embedding_dim=16,
            hypernet_reg_coeff=0.01,
            hypernet_on_the_fly_reg=False,
            hypernet_online_uniform_reg=False,
            hypernet_first_order=True,
            si_c=1.0,
            si_epsilon=0.1,
    ):
        super().__init__(
            obs_shape, action_shape, action_range, device, actor_hidden_dim, critic_hidden_dim, discount,
            init_temperature, alpha_lr, actor_lr, actor_log_std_min, actor_log_std_max, actor_update_freq, critic_lr,
            critic_tau, critic_target_update_freq, batch_size, hypernet_hidden_dim, hypernet_task_embedding_dim,
            hypernet_reg_coeff, hypernet_on_the_fly_reg, hypernet_online_uniform_reg, hypernet_first_order)

        self.si_c = si_c
        self.si_epsilon = si_epsilon

        self.params_w = {}
        self.omegas = {}
        self.prev_params = {}
        self.prev_task_params = {}

        self._save_init_params()

    def _save_init_params(self):
        # set prev_task_params as weight initializations
        for name, param in self.hypernet.weights.items():
            if param.requires_grad:
                self.prev_task_params[name] = param.detach().clone()
                self.prev_params[name] = param.detach().clone()

    def update_omegas(self):
        for name, param in self.hypernet.weights.items():
            if param.requires_grad:
                prev_param = self.prev_task_params[name]
                current_param = param.detach().clone()
                delta_param = current_param - prev_param
                current_omega = self.params_w[name] / (delta_param ** 2 + self.si_epsilon)

                self.prev_task_params[name] = current_param
                self.omegas[name] = current_omega + self.omegas.get(name, torch.zeros_like(param))

        # clear importance buffers for the next task
        self.params_w = {}

    def _estimate_importance(self):
        for name, param in self.hypernet.weights.items():
            if param.requires_grad:
                self.params_w[name] = \
                    -param.grad.detach() * (param.detach() - self.prev_params[name]) + \
                    self.params_w.get(name, torch.zeros_like(param))
                self.prev_params[name] = param.detach().clone()

    def _compute_surrogate_loss(self, named_parameters):
        assert isinstance(named_parameters, Iterable), "'named_parameters' must be a iterator"

        si_losses = []
        for name, param in named_parameters:
            if param.requires_grad:
                prev_param = self.prev_task_params[name]
                omega = self.omegas.get(name, torch.zeros_like(param))
                si_loss = torch.sum(omega * (param - prev_param) ** 2)
                si_losses.append(si_loss)

        return torch.sum(torch.stack(si_losses))

    def update(self, replay_buffer, logger, step, **kwargs):
        obs, action, reward, next_obs, not_done = replay_buffer.sample(self.batch_size)

        logger.log('train/batch_reward', reward.mean(), step)

        assert 'head_idx' in kwargs
        task_idx = kwargs.pop('head_idx')

        critic_loss = self.compute_critic_loss(obs, action, reward, next_obs, not_done,
                                               task_idx=task_idx)
        self.update_critic(critic_loss, logger, step)

        if step % self.actor_update_freq == 0:
            log_pi, actor_loss, alpha_loss = self.compute_actor_and_alpha_loss(
                obs, task_idx=task_idx)
            actor_si_surrogate_loss = self._compute_surrogate_loss(self.hypernet.weights.items())
            actor_loss = actor_loss + self.si_c * actor_si_surrogate_loss
            self.update_actor_and_alpha(log_pi, actor_loss, logger, step, alpha_loss=alpha_loss,
                                        add_reg_loss=False)

        if step % self.critic_target_update_freq == 0:
            utils.soft_update_params(self.critic, self.critic_target,
                                     self.critic_tau)

        # estimate weight importance
        self._estimate_importance()
