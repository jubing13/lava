# Copyright (C) 2014 Linaro Limited
#
# Author: Neil Williams <neil.williams@linaro.org>
#
# This file is part of LAVA Dispatcher.
#
# LAVA Dispatcher is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# LAVA Dispatcher is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along
# with this program; if not, see <http://www.gnu.org/licenses>.

import yaml
from yaml.composer import Composer
from yaml.constructor import Constructor
from lava_dispatcher.pipeline.job import Job
from lava_dispatcher.pipeline.action import (
    Pipeline,
    Action,
    Deployment,
    Boot,
    FinalizeAction,
    LavaTest,
)
from lava_dispatcher.pipeline.deployment_data import get_deployment_data
# Bring in the strategy subclass lists, ignore pylint warnings.
import lava_dispatcher.pipeline.actions.deploy.strategies  # pylint: disable=unused-import
import lava_dispatcher.pipeline.actions.boot.strategies  # pylint: disable=unused-import
import lava_dispatcher.pipeline.actions.test.strategies  # pylint: disable=unused-import


def handle_device_parameters(name, parameters, count):
    """
    Parses the action specific parameters from the device configuration
    to be added to the matching action parameters.
    name refers to the action name in the YAML.
    count refers to the number of times this action exists in the YAML file.
    Depending on the YAML structure, some methods can be a list of strings,
    some can be a list of dict objects.
    """
    if 'actions' not in parameters:
        return {}
    if name not in parameters['actions']:
        return {}
    if type(parameters['actions'][name]['methods'][count]) == str:
        return parameters['actions'][name]
    if type(parameters['actions'][name]['methods'][count]) == dict:
        return parameters['actions'][name]['methods'][count]
    return {}


class JobParser(object):
    """
    Creates a Job object from the Device and the job YAML by selecting the
    Strategy class with the highest priority for the parameters of the job.

    Adding new behaviour is a two step process:
     - always add a new Action, usually with an internal pipeline, to implement the new behaviour
     - add a new Strategy class which creates a suitable pipeline to use that Action.

    Re-use existing Action classes wherever these can be used without changes.

    If two or more Action classes have very similar behaviour, re-factor to make a
    new base class for the common behaviour and retain the specialised classes.

    Strategy selection via select() must only ever rely on the device and the
    job parameters. Add new parameters to the job to distinguish strategies, e.g.
    the boot method or deployment method.
    """

    # FIXME: needs a Schema and a check routine

    loader = None

    # annotate every object in data with line numbers so we can use
    # them is user-friendly validation messages, combined with the action.level
    # each action will also include an output_line to map to the stdout log,
    # once executed.

    def compose_node(self, parent, index):
        # the line number where the previous token has ended (plus empty lines)
        line = self.loader.line
        node = Composer.compose_node(self.loader, parent, index)
        node.__line__ = line + 1
        return node

    def construct_mapping(self, node, deep=False):
        mapping = Constructor.construct_mapping(self.loader, node, deep=deep)
        mapping['yaml_line'] = node.__line__
        return mapping

    # FIXME: add a validate() function which checks against a Schema as a completely separate step.
    def parse(self, content, device, output_dir=None):  # pylint: disable=too-many-locals
        self.loader = yaml.Loader(content)
        self.loader.compose_node = self.compose_node
        self.loader.construct_mapping = self.construct_mapping
        data = self.loader.get_single_data()

        job = Job(data)
        counts = {}
        job.device = device
        job.parameters['output_dir'] = output_dir
        pipeline = Pipeline(job=job)
        for action_data in data['actions']:
            line = action_data.pop('yaml_line', None)
            for name in action_data:
                count = counts.setdefault(name, 0)
                counts.update({name: count})
                if name == "deploy":
                    # set parameters specified in the device configuration, allow job to override.
                    parameters = handle_device_parameters(name, device.parameters, counts[name])
                    parameters.update(action_data[name])  # pass the job parameters to the instance
                    if 'os' in parameters:
                        parameters.update({'deployment_data': get_deployment_data(parameters['os'])})
                    else:
                        parameters.update({'deployment_data': get_deployment_data('oe')})
                    # allow the classmethod to check the parameters
                    deploy = Deployment.select(device, action_data[name])(pipeline, parameters)
                    deploy.action.yaml_line = line
                elif name == "boot":
                    parameters = handle_device_parameters(name, device.parameters, counts[name])
                    parameters.update(action_data[name])
                    boot = Boot.select(device, action_data[name])(pipeline, parameters)
                    boot.action.yaml_line = line
                elif name == "test":
                    # allow for multiple base tests, e.g. Android
                    parameters = handle_device_parameters(name, device.parameters, counts[name])
                    parameters.update(action_data[name])
                    test_method = LavaTest.select(device, action_data[name])(pipeline, parameters)
                else:
                    # May only end up being used for submit as other actions all need strategy method objects
                    # select the specific action of this class for this job
                    action = Action.find(name)()
                    # put parameters (like rootfs_type, results_dir) into the actions.
                    if type(action_data[name]) == dict:
                        action.parameters = action_data[name]
                    elif name == "commands":
                        # FIXME
                        pass
                    elif type(action_data[name]) == list:
                        for param in action_data[name]:
                            action.parameters = param
                    action.summary = name
                    pipeline.add_action(action)
                # uncomment for debug
                # print action.parameters

        # there's always going to need to be a finalize_process action
        pipeline.add_action(FinalizeAction())
        # the only parameters sent to the job are job parameters
        # like job_name, logging_level or target_group.
        data.pop('actions')
        data['output_dir'] = output_dir
        job.set_pipeline(pipeline)
        return job
