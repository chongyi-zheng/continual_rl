import copy
import numpy as np
import torch
import torch.nn.functional as F

import utils

from agent.sac.base_sac_agent import SacMlpAgent
from agent.sac.behavioral_cloning import BehavioralCloning
from agent.network import MultiHeadSacActorMlp, SacCriticMlp


class FisherBRCMTBCMlpCriticMultiHeadSacMlpAgent(SacMlpAgent):
    """multi-task behavioral cloning policy + single modal MLP for critic"""
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
            behavioral_cloning_hidden_dim=256,
            memory_budget=10000,
            fisher_coeff=1.0,
            reward_bonus=5.0,
    ):
        assert isinstance(action_shape, list)
        assert isinstance(action_range, list)

        self.behavioral_cloning_hidden_dim = behavioral_cloning_hidden_dim
        self.memory_budget = memory_budget
        self.fisher_coeff = fisher_coeff
        # TODO (cyzheng): reward bonus is not used currently
        self.reward_bonus = reward_bonus

        super().__init__(
            obs_shape, action_shape, action_range, device, actor_hidden_dim, critic_hidden_dim, discount,
            init_temperature, alpha_lr, actor_lr, actor_log_std_min, actor_log_std_max, actor_update_freq, critic_lr,
            critic_tau, critic_target_update_freq, batch_size)

        self.task_count = 0
        self.memories = {}
        self.union_memory = {}

    def _adjust_memory_size(self, size):
        for mem in self.memories.values():
            mem['obses'] = mem['obses'][:size]
            mem['actions'] = mem['actions'][:size]
            mem['rewards'] = mem['rewards'][:size]
            mem['next_obses'] = mem['next_obses'][:size]
            mem['not_dones'] = mem['not_dones'][:size]

    def _setup_agent(self):
        if hasattr(self, 'actor') and hasattr(self, 'critic') \
                and hasattr(self, 'optimizer'):
            return

        self.behavioral_cloning = BehavioralCloning(
            self.obs_shape, self.action_shape, self.device, self.behavioral_cloning_hidden_dim,
            multi_head=False, log_std_min=self.actor_log_std_min, log_std_max=self.actor_log_std_max
        )

        self.actor = MultiHeadSacActorMlp(
            self.obs_shape, self.action_shape, self.actor_hidden_dim,
            self.actor_log_std_min, self.actor_log_std_max
        ).to(self.device)

        self.critic = SacCriticMlp(
            self.obs_shape, self.action_shape[0], self.critic_hidden_dim
        ).to(self.device)
        self.critic_target = SacCriticMlp(
            self.obs_shape, self.action_shape[0], self.critic_hidden_dim
        ).to(self.device)

        self.reset_target_critic()

        self.log_alpha = torch.tensor(np.log(self.init_temperature)).to(self.device)
        self.log_alpha.requires_grad = True
        # set target entropy to -|A|
        self.target_entropy = -np.prod(self.action_shape[0])

        # sac optimizers
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=self.actor_lr)
        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=self.critic_lr)

        self.log_alpha_optimizer = torch.optim.Adam([self.log_alpha], lr=self.alpha_lr)

        # save initial parameters
        self._critic_init_state = copy.deepcopy(self.critic.state_dict())

    def _sample_prev_transitions(self, batch_size):
        idxs = np.random.randint(
            0, len(self.union_memory['obses']), size=batch_size
        )
        obses = self.union_memory['obses'][idxs]
        actions = self.union_memory['actions'][idxs]
        rewards = self.union_memory['rewards'][idxs]
        next_obses = self.union_memory['next_obses'][idxs]
        not_dones = self.union_memory['not_dones'][idxs]

        return obses, actions, rewards, next_obses, not_dones

    def act(self, obs, sample=False, **kwargs):
        if not isinstance(obs, torch.Tensor):
            obs = torch.Tensor(obs).to(self.device)

        with torch.no_grad():
            mu, pi, _, _ = self.actor(obs, compute_log_pi=False, **kwargs)
            action = pi if sample else mu
            assert 'head_idx' in kwargs
            action = action.clamp(*self.action_range[kwargs['head_idx']])
            assert action.ndim == 2 and action.shape[0] == 1

        return utils.to_np(action)

    def construct_memory(self, env, **kwargs):
        memory_size_per_task = self.memory_budget // (self.task_count + 1)
        self._adjust_memory_size(memory_size_per_task)

        obs = env.reset()
        self.memories[self.task_count] = {
            'obses': [],
            'actions': [],
            'rewards': [],
            'next_obses': [],
            'not_dones': [],
        }
        for _ in range(memory_size_per_task):
            with utils.eval_mode(self):
                action = self.act(obs, sample=True, **kwargs)

            next_obs, reward, done, _ = env.step(action)

            self.memories[self.task_count]['obses'].append(obs)
            self.memories[self.task_count]['actions'].append(action)
            self.memories[self.task_count]['rewards'].append(reward)
            self.memories[self.task_count]['next_obses'].append(next_obs)
            not_done = np.array([not done_ for done_ in done], dtype=np.float32)
            self.memories[self.task_count]['not_dones'].append(not_done)

            obs = next_obs

        self.memories[self.task_count]['obses'] = torch.Tensor(
            self.memories[self.task_count]['obses']).to(device=self.device)
        self.memories[self.task_count]['actions'] = torch.Tensor(
            self.memories[self.task_count]['actions']).to(device=self.device)
        self.memories[self.task_count]['rewards'] = torch.Tensor(
            self.memories[self.task_count]['rewards']).to(device=self.device).unsqueeze(-1)
        self.memories[self.task_count]['next_obses'] = torch.Tensor(
            self.memories[self.task_count]['next_obses']).to(device=self.device)
        self.memories[self.task_count]['not_dones'] = torch.Tensor(
            self.memories[self.task_count]['not_dones']).to(device=self.device).unsqueeze(-1)

        # merge transitions in memories
        self.union_memory = {}
        for mem in self.memories.values():
            for k, v in mem.items():
                self.union_memory[k] = torch.cat([
                    self.union_memory.get(
                        k, torch.empty([0] + list(v.shape[1:]), device=self.device)),
                    v], dim=0)

        self.task_count += 1

    def train_bc(self, train_steps, step, logger):
        for train_step in range(train_steps):
            self.behavioral_cloning.update_learning_rate(train_step)

            obses, actions, _, _, _ = self._sample_prev_transitions(
                self.batch_size)
            self.behavioral_cloning.update(obses, actions, logger, train_step + step)

    def compute_critic_loss(self, obs, action, reward, next_obs, not_done, **kwargs):
        assert 'prev_obs' in kwargs, "We should use observations of previous tasks " \
                                     "to compute critic fisher regularization"
        prev_obs = kwargs.pop('prev_obs')

        with torch.no_grad():
            _, next_policy_action, next_log_pi, _ = self.actor(next_obs, **kwargs)
            target_Q1, target_Q2 = self.critic_target(next_obs, next_policy_action, **kwargs)
            target_V = torch.min(target_Q1,
                                 target_Q2) - self.alpha.detach() * next_log_pi
            target_Q = reward + (not_done * self.discount * target_V)
        current_Q1, current_Q2 = self.critic(obs, action, **kwargs)

        # regularize with observations of previous tasks
        if prev_obs is not None:
            _, policy_action, _, _ = self.actor(prev_obs, **kwargs)
            reg_Q1, reg_Q2 = self.critic(prev_obs, policy_action)
            log_mu, _ = self.behavioral_cloning.policy.log_probs(prev_obs, policy_action)

            # (cyzheng) reference: equation 10 in http://arxiv.org/abs/2103.08050.
            mu_grads = torch.autograd.grad(log_mu.sum(), policy_action)[0]
            # (cyzheng): create graph for second order derivatives
            reg_Q1_grads = torch.autograd.grad(
                reg_Q1.sum(), policy_action, create_graph=True)[0]
            reg_Q2_grads = torch.autograd.grad(
                reg_Q2.sum(), policy_action, create_graph=True)[0]
            grad_diff1_norm = torch.sum(torch.square(mu_grads - reg_Q1_grads), dim=-1)
            grad_diff2_norm = torch.sum(torch.square(mu_grads - reg_Q2_grads), dim=-1)
            q_reg = torch.mean(grad_diff1_norm + grad_diff2_norm)
        else:
            q_reg = torch.tensor(0, device=self.device)

        critic_loss = F.mse_loss(current_Q1, target_Q) + F.mse_loss(current_Q2, target_Q) + \
                      self.fisher_coeff * q_reg

        return critic_loss

    def update(self, replay_buffer, logger, step, **kwargs):
        obs, action, reward, next_obs, not_done = replay_buffer.sample(
            self.batch_size)
        if self.task_count > 0:
            prev_obs, _, _, _, _ = self._sample_prev_transitions(self.batch_size)
        else:
            prev_obs = None

        logger.log('train/batch_reward', reward.mean(), step)

        critic_loss = self.compute_critic_loss(obs, action, reward, next_obs, not_done,
                                               prev_obs=prev_obs, **kwargs)
        self.update_critic(critic_loss, logger, step)

        if step % self.actor_update_freq == 0:
            log_pi, actor_loss, alpha_loss = self.compute_actor_and_alpha_loss(obs, **kwargs)
            self.update_actor_and_alpha(log_pi, actor_loss, logger, step, alpha_loss=alpha_loss)

        if step % self.critic_target_update_freq == 0:
            utils.soft_update_params(self.critic, self.critic_target,
                                     self.critic_tau)
