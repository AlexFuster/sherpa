"""
SHERPA is a Python library for hyperparameter tuning of machine learning models.
Copyright (C) 2018  Lars Hertel, Peter Sadowski, and Julian Collado.

This file is part of SHERPA.

SHERPA is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

SHERPA is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with SHERPA.  If not, see <http://www.gnu.org/licenses/>.
"""
import os
import random
import numpy
import logging
import sherpa
import pandas
import scipy.stats
import scipy.optimize
import sklearn.gaussian_process
from sherpa.core import Choice, Continuous, Discrete, Ordinal, AlgorithmState
import sklearn.model_selection
from sklearn import preprocessing
import warnings
import collections


alglogger = logging.getLogger(__name__)


class Algorithm(object):
    """
    Abstract algorithm that generates new set of parameters.
    """
    def get_suggestion(self, parameters, results, lower_is_better):
        """
        Returns a suggestion for parameter values.

        Args:
            parameters (list[sherpa.Parameter]): the parameters.
            results (pandas.DataFrame): all results so far.
            lower_is_better (bool): whether lower objective values are better.

        Returns:
            dict: parameter values.
        """
        raise NotImplementedError("Algorithm class is not usable itself.")

    def load(self, num_trials):
        """
        Reinstantiates the algorithm when loaded.

        Args:
            num_trials (int): number of trials in study so far.
        """
        pass

    def get_best_result(self, parameters, results, lower_is_better):
        # Get best result so far
        best_idx = (results.loc[:, 'Objective'].idxmin()
                    if lower_is_better
                    else results.loc[:, 'Objective'].idxmax())

        if not numpy.isfinite(best_idx):
            # Can happen if there are no valid results,
            # best_idx=nan when results are nan.
            alglogger.warning('Empty results file! Returning empty dictionary.')
            return {}

        best_result = results.loc[best_idx, :].to_dict()
        best_result.pop('Status')
        return best_result


class Repeat(Algorithm):
    """
    Takes another algorithm and repeats every hyperparameter configuration a
    given number of times. The wrapped algorithm will be passed the mean
    objective values of the repeated experiments.

    Args:
        algorithm (sherpa.algorithms.Algorithm): the algorithm to produce
            hyperparameter configurations.
        num_times (int): the number of times to repeat each configuration.
        wait_for_completion (bool): only relevant when running in parallel with
            max_concurrent > 1. Means that the algorithm won't issue the next
            suggestion until all repetitions are completed. This can be useful
            when the repeats have impact on sequential decision making in the
            wrapped algorithm.
    """
    def __init__(self, algorithm, num_times=5, wait_for_completion=False):
        self.algorithm = algorithm
        self.num_times = num_times
        self.queue = []
        self.prev_completed = 0
        self.wait_for_completion = wait_for_completion

    def get_suggestion(self, parameters, results=None, lower_is_better=True):
        if len(self.queue) == 0:
            if results is not None and len(results) > 0:
                completed = results.query("Status == 'COMPLETED'")
                if (self.wait_for_completion and len(
                        completed) < self.prev_completed + self.num_times):
                    return AlgorithmState.WAIT
                self.prev_completed += self.num_times
                aggregate_results = completed.groupby([p.name for p in parameters]
                                                    + ['Status']) \
                                             .agg(['mean', 'var', 'count']) \
                                             .loc[:, 'Objective'] \
                                             .reset_index() \
                                             .assign(varObjective=lambda x: x['var'] / x['count']) \
                                             .rename({'mean': 'Objective'},
                                                     axis=1) \
                                             .drop('var', axis=1) \
                                             .query("count >= {}".format(
                                                                self.num_times))
            else:
                aggregate_results = None
            suggestion = self.algorithm.get_suggestion(parameters=parameters,
                                                       results=aggregate_results,
                                                       lower_is_better=lower_is_better)
            self.queue += [suggestion] * self.num_times

        return self.queue.pop()


class RandomSearch(Algorithm):
    """
    Random Search with a repeat option.

    Trials parameter configurations are uniformly sampled from their parameter
    ranges. The repeat option allows to re-run a trial `repeat` number of times.
    By default this is 1.

    Args:
        max_num_trials (int): number of trials, otherwise runs indefinitely.
        repeat (int): number of times to repeat a parameter configuration.
    """
    def __init__(self, max_num_trials=None, repeat=1):
        self.i = 0  # number of sampled configs
        self.n = max_num_trials or 2**32  # total number of configs to be sampled
        self.m = repeat  # number of times to repeat each config
        self.j = 0  # number of trials submitted with this config
        self.theta_i = {}  # current parameter config

    def get_suggestion(self, parameters, results=None, lower_is_better=True):
        # If number of repetitions are reached set them back to zero
        if self.j == self.m:
            self.j = 0

        # If there are no repetitions yet, sample a new config
        if self.j == 0:
            self.theta_i = {p.name: p.sample() for p in parameters}
            self.i += 1

        # If the maximum number of configs is reached, return None
        if self.i > self.n:
            return None
        # Else increase the count of this config by one and return it
        else:
            self.j += 1
            return self.theta_i


                

class Iterate(Algorithm):
    """
    Iterate over a set of fully-specified hyperparameter combinations.
    
    Args:
        hp_iter (list): list of fully-specified hyperparameter dicts. 
    """
    def __init__(self, hp_iter):
        self.hp_iter = hp_iter
        self.count = 0
        
        # Make sure all hyperparameter values are specified.
        parameters = self.get_parameters()
    
    def get_suggestion(self, parameters, results=None, lower_is_better=True):
        if self.count >= len(self.hp_iter):
            # No more combinations to try.
            return None
        else:
            hp = self.hp_iter[self.count]
            self.count += 1
            return hp

    def load(self, num_trials):
        self.count = num_trials
        
    def get_parameters(self):
        """
        Computes list of parameter objects from list of hyperparameter
        combinations, which is needed for initializing a Study.
        
        Returns:
            list: List of Parameter objects.
        """
        parameters = []
        keys = self.hp_iter[0].keys()
        for pname in keys:
            # Get unique values of this (possibly unhashable) parameter.
            prange = []
            for i,hp in enumerate(self.hp_iter):
                if pname not in hp:
                    raise Exception('Parameter {} not found in list item {}.'.format(pname, i))
                value = hp[pname]
                if value not in prange:
                    prange.append(value)
            p = sherpa.Parameter.from_dict({'name': pname,
                                     'type': 'choice',
                                     'range': prange})
            parameters.append(p)
        return parameters


class GridSearch(Algorithm):
    """
    Explores a grid across the hyperparameter space such that every pairing is
    evaluated.

    For continuous and discrete parameters grid points are picked within the
    range. For example, a continuous parameter with range [1, 2] with two grid
    points would have points 1 1/3 and 1 2/3. For three points, 1 1/4, 1 1/2,
    and 1 3/4.
    
    Example:
    ::

        hp_space = {'act': ['tanh', 'relu'],
                    'lrinit': [0.1, 0.01],
                    }
        parameters = sherpa.Parameter.grid(hp_space)
        alg = sherpa.algorithms.GridSearch()

    Args:
        num_grid_points (int): number of grid points for continuous / discrete.

    """
    def __init__(self, num_grid_points=2, repeat=1):
        self.grid = None
        self.num_grid_points = num_grid_points
        self.i = 0  # number of sampled configs
        self.m = repeat  # number of times to repeat each config
        self.j = 0  # number of trials submitted with this config
        self.theta_i = {}  # current parameter config

    def get_suggestion(self, parameters, results=None, lower_is_better=True):
        if self.i == 0 and self.j == 0:
            param_dict = self._get_param_dict(parameters)
            self.grid = list(sklearn.model_selection.ParameterGrid(param_dict))
        
        # If number of repetitions are reached set them back to zero
        if self.j == self.m:
            self.j = 0
            self.i += 1

        # If the maximum number of configs is reached, return None
        if self.i == len(self.grid):
            return None
        # Else increase the count of this config by one and return it
        else:
            # If there are no repetitions yet, get a new config
            if self.j == 0:
                self.theta_i = self.grid[self.i]
            
            self.j += 1
            return self.theta_i

    def _get_param_dict(self, parameters):
        param_dict = {}
        for p in parameters:
            if isinstance(p, Continuous) or isinstance(p, Discrete):
                values = []
                for i in range(self.num_grid_points):
                    if p.scale == 'log':
                        v = numpy.log10(p.range[1]) - numpy.log10(p.range[0])
                        v *= (i + 1) / (self.num_grid_points + 1)
                        v += numpy.log10(p.range[0])
                        v = 10**v
                        if isinstance(p, Discrete):
                            v = int(v)
                        values.append(v)
                    else:
                        v = p.range[1]-p.range[0]
                        v *= (i + 1)/(self.num_grid_points + 1)
                        v += p.range[0]
                        if isinstance(p, Discrete):
                            v = int(v)
                        values.append(v)
            else:
                values = p.range
            param_dict[p.name] = values
        return param_dict


class LocalSearch(Algorithm):
    """
    Local Search Algorithm.

    This algorithm expects to start with a very good hyperparameter
    configuration. It changes one hyperparameter at a time to see if better
    results can be obtained.

    Args:
        seed_configuration (dict): hyperparameter configuration to start with.
        perturbation_factors (Union[tuple,list]): continuous parameters will be
            multiplied by these.
        repeat_trials (int): number of times that identical configurations are
            repeated to test for random fluctuations.
    """
    def __init__(self, seed_configuration, perturbation_factors=(0.8, 1.2), repeat_trials=1):
        self.seed_configuration = seed_configuration
        self.count = 0
        self.submitted = []
        self.perturbation_factors = perturbation_factors
        self.next_trial = []
        self.repeat_trials = repeat_trials
        
    def get_suggestion(self, parameters, results, lower_is_better):
        if not self.next_trial:
            self.next_trial = self._get_next_trials(parameters, results,
                                                   lower_is_better)

        return self.next_trial.pop()

    def _get_next_trials(self, parameters, results, lower_is_better):
        self.count += 1
        if self.count == 1:
            self.submitted.append(self.seed_configuration)
            return [self.seed_configuration] * self.repeat_trials

        # Get best result so far
        if len(results) > 0:
            completed = results.query("Status == 'COMPLETED'")
            if len(completed) > 0:
                best_idx = (completed.loc[:, 'Objective'].idxmin() if lower_is_better
                            else completed.loc[:, 'Objective'].idxmax())
                self.seed_configuration = completed.loc[
                    best_idx, [p.name for p in parameters]].to_dict()

        # Randomly sample perturbations and return first that hasn't been tried
        for param in random.sample(parameters, len(parameters)):
            if isinstance(param, Choice):
                values = random.sample(param.range,
                                       len(param.range))
                for val in values:
                    new_params = self.seed_configuration.copy()
                    new_params[param.name] = val
                    if new_params not in self.submitted:
                        self.submitted.append(new_params)
                        return [new_params] * self.repeat_trials
            else:
                for incr in random.sample([True, False], 2):
                    new_params = self._perturb(candidate=self.seed_configuration.copy(),
                                               parameter=param,
                                               increase=incr)
                    if new_params not in self.submitted:
                        self.submitted.append(new_params)
                        return [new_params] * self.repeat_trials
        else:
            alglogger.info("All local perturbations have been exhausted and "
                           "no better local optimum was found.")
            return [None] * self.repeat_trials

    def _perturb(self, candidate, parameter, increase):
        """
        Randomly choose one parameter and perturb it.

        For Ordinal this is increased/decreased, for continuous/discrete this is
        times 0.8/1.2.

        Args:
            parameters (list[sherpa.core.Parameter]): parameter ranges.
            configuration (dict): a parameter configuration to be perturbed.
            param_name (str): the name of the parameter to perturb.
            increase (bool): whether to increase or decrease the parameter.

        Returns:
            dict: perturbed configuration
        """
        if isinstance(parameter, Ordinal):
            shift = +1 if increase else -1
            values = parameter.range
            newidx = values.index(candidate[parameter.name]) + shift
            newidx = numpy.clip(newidx, 0, len(values) - 1)
            candidate[parameter.name] = values[newidx]

        else:
            factor = self.perturbation_factors[1 if increase else 0]
            candidate[parameter.name] *= factor

            if isinstance(parameter, Discrete):
                candidate[parameter.name] = int(candidate[parameter.name])

            candidate[parameter.name] = numpy.clip(candidate[parameter.name],
                                               min(parameter.range),
                                               max(parameter.range))
        return candidate


class StoppingRule(object):
    """
    Abstract class to evaluate whether a trial should stop conditional on all
    results so far.
    """
    def should_trial_stop(self, trial, results, lower_is_better):
        """
        Args:
            trial (sherpa.Trial): trial to be stopped.
            results (pandas.DataFrame): all results so far.
            lower_is_better (bool): whether lower objective values are better.

        Returns:
            bool: decision.
        """
        raise NotImplementedError("StoppingRule class is not usable itself.")


class MedianStoppingRule(StoppingRule):
    """
    Median Stopping-Rule similar to Golovin et al.
    "Google Vizier: A Service for Black-Box Optimization".

    * For a Trial `t`, the best objective value is found.
    * Then the best objective value for every other trial is found.
    * Finally, the best-objective for the trial is compared to the median of
      the best-objectives of all other trials.

    If trial `t`'s best objective is worse than that median, it is
    stopped.

    If `t` has not reached the minimum iterations or there are not
    yet the requested number of comparison trials, `t` is not
    stopped. If `t` is all nan's it is stopped by default.

    Args:
        min_iterations (int): the minimum number of iterations a trial runs for
            before it is considered for stopping.
        min_trials (int): the minimum number of comparison trials needed for a
            trial to be stopped.
    """
    def __init__(self, min_iterations=0, min_trials=1):
        self.min_iterations = min_iterations
        self.min_trials = min_trials

    def should_trial_stop(self, trial, results, lower_is_better):
        """
        Args:
            trial (sherpa.Trial): trial to be stopped.
            results (pandas.DataFrame): all results so far.
            lower_is_better (bool): whether lower objective values are better.

        Returns:
            bool: decision.
        """
        if len(results) == 0:
            return False
        
        trial_rows = results.loc[results['Trial-ID'] == trial.id]
        
        max_iteration = trial_rows['Iteration'].max()
        if max_iteration < self.min_iterations:
            return False
        
        trial_obj_val = trial_rows['Objective'].min() if lower_is_better else trial_rows['Objective'].max()

        if numpy.isnan(trial_obj_val) and not trial_rows.empty:
            alglogger.debug("Value {} is NaN: {}, trial rows: {}".format(trial_obj_val, numpy.isnan(trial_obj_val), trial_rows))
            return True

        other_trial_ids = set(results['Trial-ID']) - {trial.id}
        comparison_vals = []

        for tid in other_trial_ids:
            trial_rows = results.loc[results['Trial-ID'] == tid]
            
            max_iteration = trial_rows['Iteration'].max()
            if max_iteration < self.min_iterations:
                continue

            valid_rows = trial_rows.loc[trial_rows['Iteration'] <= max_iteration]
            obj_val = valid_rows['Objective'].min() if lower_is_better else valid_rows['Objective'].max()
            comparison_vals.append(obj_val)

        if len(comparison_vals) < self.min_trials:
            return False

        if lower_is_better:
            decision = trial_obj_val > numpy.nanmedian(comparison_vals)
        else:
            decision = trial_obj_val < numpy.nanmedian(comparison_vals)

        return decision


def get_sample_results_and_params():
    """
    Call as:
    ::

        parameters, results, lower_is_better = sherpa.algorithms.get_sample_results_and_params()


    to get a sample set of parameters, results and lower_is_better variable.
    Useful for algorithm development.

    Note: losses are obtained from
    ::

        loss = param_a / float(iteration + 1) * param_b

    """
    here = os.path.abspath(os.path.dirname(__file__))
    results = pandas.read_csv(os.path.join(here, "sample_results.csv"), index_col=0)
    parameters = [Choice(name="param_a",
                         range=[1, 2, 3]),
                  Continuous(name="param_b",
                         range=[0, 1])]
    lower_is_better = True
    return parameters, results, lower_is_better


class PopulationBasedTraining(Algorithm):
    """
    Population based training (PBT) as introduced by Jaderberg et al. 2017.

    PBT trains a generation of ``population_size`` seed trials (randomly initialized) for a user
    specified number of iterations. After that the same number of trials are
    sampled from the top 33% of the seed generation. Those trials are perturbed
    in their hyperparameter configuration and continue training. After that
    trials are sampled from that generation etc.

    Args:
        population_size (int): the number of randomly intialized trials at the
            beginning and number of concurrent trials after that.
        parameter_range (dict[Union[list,tuple]): upper and lower bounds beyond
            which parameters cannot be perturbed.
        perturbation_factors (tuple[float]): the factors by which continuous
            parameters are multiplied upon perturbation; one is sampled randomly
            at a time.
    """
    def __init__(self, population_size=20, parameter_range={},
                 perturbation_factors=(0.8, 1.0, 1.2)):
        self.population_size = population_size
        self.parameter_range = parameter_range
        self.perturbation_factors = perturbation_factors
        self.generation = 0
        self.count = 0
        self.random_sampler = RandomSearch()

    def get_suggestion(self, parameters, results, lower_is_better):
        self.count += 1
        self.generation = (self.count - 1)//self.population_size + 1

        if self.generation == 1:
            trial = self.random_sampler.get_suggestion(parameters,
                                                       results, lower_is_better)
            trial['lineage'] = ''
            trial['load_from'] = ''
            trial['save_to'] = str(self.count)
        else:
            trial = self._truncation_selection(parameters=parameters,
                                               results=results,
                                               lower_is_better=lower_is_better)
            trial['load_from'] = str(int(trial['save_to']))
            trial['save_to'] = str(int(self.count))
            trial['lineage'] += trial['load_from'] + ','
        trial['generation'] = self.generation
        return trial

    def _truncation_selection(self, parameters, results, lower_is_better):
        """
        Continues the top 80% of the generation, resamples the rest from the
        top 20% and perturbs.

        Returns
            dict: parameter dictionary.
        """
        # Select correct generation and sort generation members
        completed = results.loc[results['Status'] == 'COMPLETED', :]
        generation_df = completed.loc[(completed.generation
                                       == self.generation - 1), :]\
                                 .sort_values(by='Objective',
                                              ascending=lower_is_better)

        if (self.count - 1) % self.population_size / self.population_size < 0.8:
            # Go through top 80% of generation
            d = generation_df.iloc[(self.count - 1) % self.population_size].to_dict()
        else:
            # For the rest, sample from top 20% and perturb
            idx = numpy.random.randint(low=0, high=self.population_size//5)
            d = generation_df.iloc[idx].to_dict()
            d = self._perturb(candidate=d, parameters=parameters)
        trial = {param.name: d[param.name] for param in parameters}
        for key in ['load_from', 'save_to', 'lineage']:
            trial[key] = d[key]
        return trial

    def _perturb(self, candidate, parameters):
        """
        Randomly perturbs candidate parameters by perturbation factors.

        Args:
            candidate (dict): candidate parameter configuration.
            parameters (list[sherpa.core.Parameter]): parameter ranges.

        Returns:
            dict: perturbed parameter configuration.
        """
        for param in parameters:
            if isinstance(param, Continuous) or isinstance(param, Discrete):
                factor = numpy.random.choice(self.perturbation_factors)
                candidate[param.name] *= factor

                if isinstance(param, Discrete):
                    candidate[param.name] = int(candidate[param.name])

                candidate[param.name] = numpy.clip(candidate[param.name],
                                                   min(self.parameter_range.get(param.name) or param.range),
                                                   max(self.parameter_range.get(param.name) or param.range))

            elif isinstance(param, Ordinal):
                shift = numpy.random.choice([-1, 0, +1])
                values = self.parameter_range.get(param.name) or param.range
                newidx = values.index(candidate[param.name]) + shift
                newidx = numpy.clip(newidx, 0, len(values)-1)
                candidate[param.name] = values[newidx]

            elif isinstance(param, Choice):
                candidate[param.name] = param.sample()

            else:
                raise ValueError("Unrecognized Parameter Object.")

        return candidate


class Genetic(Algorithm):
    def __init__(self, mutation_rate=0.1, max_num_trials=None):
        self.mutation_rate = mutation_rate
        self.max_num_trials = max_num_trials
        self.count = 0

    def get_suggestion(self, parameters, results, lower_is_better):
        """
        Create a new parameter value as a random mixture of some of the best
        trials and sampling from original distribution.

        Return
        dict: parameter values dictionary
        """
        if self.max_num_trials and self.count >= self.max_num_trials:
            return None
        # Choose 2 of the top trials and get their parameter values
        trial_1_params = self._get_candidate(parameters, results,
                                             lower_is_better)
        trial_2_params = self._get_candidate(parameters, results,
                                             lower_is_better)
        params_values_for_next_trial = {}
        for param_name in trial_1_params.keys():
            param_origin = numpy.random.random()  # randomly choose where to get the value from
            if param_origin < self.mutation_rate:  # Use mutation
                for parameter_object in parameters:
                    if param_name == parameter_object.name:
                        params_values_for_next_trial[
                            param_name] = parameter_object.sample()
            elif (self.mutation_rate <= param_origin and param_origin < self.mutation_rate + (1 - self.mutation_rate) / 2):
                params_values_for_next_trial[param_name] = trial_1_params[
                    param_name]
            else:
                params_values_for_next_trial[param_name] = trial_2_params[
                    param_name]
        self.count += 1
        return params_values_for_next_trial

    def _get_candidate(self, parameters, results, lower_is_better,
                       min_candidates=10):
        """
        Samples candidates parameters from the top 33% of population. If less than min_candidates
        then use a random sample

        Returns
        dict: parameter dictionary.
        """
        if results.shape[0] > 0:
            population = results.loc[results['Status'] != 'INTERMEDIATE',
                         :]  # select only completed trials
        else:
            population = None
        if population is None or population.shape[0] < min_candidates:
            trial_param_values = {}
            for parameter_object in parameters:
                trial_param_values[
                    parameter_object.name] = parameter_object.sample()
            return trial_param_values
        population = population.sort_values(by='Objective',
                                            ascending=lower_is_better)
        idx = numpy.random.randint(low=0, high=population.shape[
                                                   0] // 3)  # pick randomly among top 33%
        trial_all_values = population.iloc[
            idx].to_dict()  # extract the trial values on results table
        trial_param_values = {param.name: trial_all_values[param.name] for param
                              in parameters}  # Select only parameter values
        return trial_param_values
