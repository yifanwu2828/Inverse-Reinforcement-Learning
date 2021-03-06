import abc
import itertools
from typing import Tuple, Optional, Dict

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F
from torch import optim
from torch import distributions

try:
    from icecream import ic
    from icecream import install
    install()
except ImportError:  # Graceful fallback if IceCream isn't installed.
    ic = lambda *a: None if not a else (a[0] if len(a) == 1 else a)  # noqa

from gcl.infrastructure import utils
import gcl.infrastructure.pytorch_util as ptu
from gcl.policies.base_policy import BasePolicy


class MLPPolicy(BasePolicy, nn.Module, metaclass=abc.ABCMeta):
    """
    Policy for predicting a Gaussian action distribution
    """

    def __init__(self, ac_dim, ob_dim, n_layers, size,
                 discrete=False,
                 learning_rate=1e-4, training=True,
                 nn_baseline=False,
                 **kwargs):
        super().__init__(**kwargs)

        # init vars
        self.ac_dim = ac_dim
        self.ob_dim = ob_dim
        self.n_layers = n_layers
        # TODO: may need to change size accordingly
        self.size = 64  # size
        self.discrete = discrete
        self.learning_rate = learning_rate
        self.training = training
        self.nn_baseline = nn_baseline

        # Discrete action space
        if self.discrete:
            self.mean_net = None  # using in continuous action space
            self.logstd = None  # using in continuous action space

            self.logits_na = ptu.build_mlp(input_size=self.ob_dim,
                                           output_size=self.ac_dim,
                                           n_layers=self.n_layers,
                                           size=self.size)
            # init weight
            self.logits_na.apply(ptu.initialize_weights)

            # To GPU if available
            self.logits_na.to(ptu.device)

            self.optimizer = optim.Adam(self.logits_na.parameters(),
                                        self.learning_rate)

        # Continuous action space
        else:
            self.logits_na = None  # using in discrete action space

            self.mean_net = ptu.build_mlp(input_size=self.ob_dim,
                                          output_size=self.ac_dim,
                                          n_layers=self.n_layers, size=self.size)
            self.logstd = nn.Parameter(                 # change no_gpu to train on cpu
                torch.zeros(self.ac_dim, dtype=torch.float32, device=ptu.device)
            )
            # init weight
            self.mean_net.apply(ptu.initialize_weights)

            # To GPU if available
            self.mean_net.to(ptu.device)
            self.logstd.to(ptu.device)

            self.optimizer = optim.Adam(
                itertools.chain([self.logstd], self.mean_net.parameters()),
                lr=self.learning_rate
            )

        # Baseline
        if nn_baseline:
            self.baseline = ptu.build_mlp(
                input_size=self.ob_dim,
                output_size=1,
                n_layers=self.n_layers,
                size=self.size,
            )
            # init weight
            self.baseline.apply(ptu.initialize_weights)

            # To GPU if available
            self.baseline.to(ptu.device)

            self.baseline_optimizer = optim.Adam(
                self.baseline.parameters(),
                self.learning_rate,
            )
        else:
            self.baseline = None

    ##################################
    ##################################

    ##################################

    def save(self, filepath):
        """ Save Model's state_dict """
        torch.save(self.state_dict(), filepath)

    ##################################
    def get_action(self, obs: np.ndarray, with_logprob=True) -> Tuple[np.ndarray, Optional[np.ndarray]]:
        """
        Query the policy with observation(s) to get selected action(s)
        and the corresponding log probability
        :param obs: observation
        :param with_logprob: whether to output log_prob or not
        :return: action, log_prob
        """
        if len(obs.shape) > 1:
            observation = obs
        else:
            observation = obs[None]

        # Return the action that the policy prescribes
        observation = ptu.from_numpy(observation.astype(np.float32))
        action_dist = self(observation)
        action = action_dist.rsample()
        if with_logprob:
            log_prob = action_dist.log_prob(action)
        else:
            log_prob = None
        return ptu.to_numpy(action), ptu.to_numpy(log_prob)

    # update/train this policy
    def update(self, observations, actions, **kwargs) -> dict:
        raise NotImplementedError

    def forward(self, observation: torch.FloatTensor) -> distributions.Distribution:
        """
        Returns the action distribution
        param: observation
        return: pi_dist
        type: torch.distributions.Distribution
        """
        if self.discrete:
            logits = self.logits_na(observation)
            pi_dist = distributions.Categorical(logits=logits)
        else:
            mean = self.mean_net(observation)
            std = self.logstd.exp()
            pi_dist = distributions.MultivariateNormal(mean, torch.diag(std))

        return pi_dist


#####################################################
#####################################################

class MLPPolicyPG(MLPPolicy):
    """
    Policy that uses policy gradient to update parameters
    """

    def __init__(self, ac_dim, ob_dim, n_layers, size, discrete, **kwargs):
        super().__init__(ac_dim, ob_dim, n_layers, size, **kwargs)
        self.discrete = discrete
        # Init baseline_loss
        self.baseline_loss = nn.MSELoss()

        self.mean_net.to(ptu.device)
        self.baseline.to(ptu.device)
        self.logstd.to(ptu.device)

        ic("-----MLP Policy------")
        ic(self.ac_dim)
        ic(self.ob_dim)
        ic(self.n_layers)
        ic(self.size)
        ic(self.nn_baseline)

    def __repr__(self):
        return f"{self.__class__.__name__}"

    def update(self,
               observations: np.ndarray,
               actions: np.ndarray,
               advantages: np.ndarray,
               q_values: Optional[np.ndarray] = None
               ) -> Dict[str, np.ndarray]:
        """
        Update Policy
        :param observations:
        :param actions:
        :param advantages
        :param q_values:
        """
        observations = ptu.from_numpy(observations)
        actions = ptu.from_numpy(actions)
        advantages = ptu.from_numpy(advantages)

        # HINT1: Recall that the expression that we want to MAXIMIZE
        # is the expectation over collected trajectories of:
        # sum_{t=0}^{T-1} [grad [log pi(a_t|s_t) * (Q_t - b_t)]]

        action_dist = self(observations)

        # log_prob is negative
        log_prob: torch.Tensor = action_dist.log_prob(actions)
        assert log_prob.size() == advantages.size()

        # advantage = Q-V should be positive indicate the traj is better than average of traj
        loss = -torch.mean(log_prob * advantages)
        # self.optimizer.zero_grad(set_to_none=True)
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        train_log = {'Training_Policy_Loss': ptu.to_numpy(loss)}

        # Apply baseline to reduce variance
        if self.nn_baseline and q_values is not None:
            # Normalize the q_values to have a mean of zero and a standard deviation of one
            targets = utils.normalize(q_values, q_values.mean(), q_values.std())
            targets = torch.squeeze(ptu.from_numpy(targets))

            # Avoid any subtle broadcasting bugs that can arise when dealing with arrays of shape
            # [ N ] versus shape [ N x 1 ], use `squeeze`  to remove dimensions of size 1
            baseline_predictions = torch.squeeze(self.baseline(observations))
            assert baseline_predictions.shape == targets.shape

            baseline_loss = F.mse_loss(baseline_predictions, targets)
            # self.baseline_optimizer.zero_grad(set_to_none=True)
            self.baseline_optimizer.zero_grad()
            baseline_loss.backward()
            self.baseline_optimizer.step()

        return train_log

    def run_baseline_prediction(self, obs: np.ndarray) -> np.ndarray:
        """
        Helper function that converts `obs` to a tensor,
        calls the forward method of the baseline MLP,
        and returns a numpy array

        :param: obs: np.ndarray of size [N, 1]
        :returns: np.ndarray of size [N]
        """
        obs = ptu.from_numpy(obs)
        predictions = self.baseline(obs)
        return ptu.to_numpy(predictions)[:, 0]

# TODO: implement MLPPolicyGPS()
# class MLPPolicyGPS(MLPPolicy):
#     """
#     Policy that uses guided policy search to update parameters
#     """
#
#     def __init__(self):
#         super().__init__()
#         raise NotImplementedError
