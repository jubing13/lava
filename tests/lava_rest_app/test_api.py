# -*- coding: utf-8 -*-
# Copyright (C) 2018-2019 Linaro Limited
#
# Author: Milosz Wasilewski <milosz.wasilewski@linaro.org>
#
# This file is part of LAVA.
#
# LAVA is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License version 3
# as published by the Free Software Foundation
#
# LAVA is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with LAVA.  If not, see <http://www.gnu.org/licenses/>.

import csv
import json
import pathlib
import pytest
import xml.etree.ElementTree as ET

from datetime import timedelta
from django.conf import settings
from django.contrib.auth.models import User, Group
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from lava_common.version import __version__
from lava_common.compat import yaml_load
from lava_scheduler_app.models import (
    Alias,
    Device,
    DeviceType,
    GroupDeviceTypePermission,
    GroupDevicePermission,
    GroupWorkerPermission,
    Tag,
    TestJob,
    Worker,
)
from lava_results_app import models as result_models
from linaro_django_xmlrpc.models import AuthToken

from lava_rest_app import versions


EXAMPLE_JOB = """
job_name: test
visibility: public
timeouts:
  job:
    minutes: 10
  action:
    minutes: 5
actions: []
protocols: {}
"""

EXAMPLE_WORKING_JOB = """
device_type: public_device_type1
job_name: test
visibility: public
timeouts:
  job:
    minutes: 10
  action:
    minutes: 5
actions: []
protocols: {}
"""

LOG_FILE = """
- {"dt": "2018-10-03T16:28:28.199903", "lvl": "info", "msg": "lava-dispatcher, installed at version: 2018.7-1+stretch"}
- {"dt": "2018-10-03T16:28:28.200807", "lvl": "info", "msg": "start: 0 validate"}
"""


class TestRestApi:
    @pytest.fixture(autouse=True)
    def setUp(self, db):
        self.version = versions.versions[-1]  # use latest version by default

        # create users
        self.admin = User.objects.create(username="admin", is_superuser=True)
        self.admin_pwd = "supersecret"
        self.admin.set_password(self.admin_pwd)
        self.admin.save()
        self.user = User.objects.create(username="user1")
        self.user_pwd = "supersecret"
        self.user.set_password(self.user_pwd)
        self.user.save()
        self.user_no_token_pwd = "secret"
        self.user_no_token = User.objects.create(username="user2")
        self.user_no_token.set_password(self.user_no_token_pwd)
        self.user_no_token.save()

        self.group1 = Group.objects.create(name="group1")
        self.group2 = Group.objects.create(name="group2")
        self.user.groups.add(self.group2)
        admintoken = AuthToken.objects.create(  # nosec - unit test support
            user=self.admin, secret="adminkey"
        )
        self.usertoken = AuthToken.objects.create(  # nosec - unit test support
            user=self.user, secret="userkey"
        )
        # create second token to check whether authentication still works
        AuthToken.objects.create(user=self.user, secret="userkey2")  # nosec - unittest

        self.userclient = APIClient()
        self.userclient.credentials(HTTP_AUTHORIZATION="Token " + self.usertoken.secret)
        self.userclient_no_token = APIClient()
        self.adminclient = APIClient()
        self.adminclient.credentials(HTTP_AUTHORIZATION="Token " + admintoken.secret)

        # Create workers
        self.worker1 = Worker.objects.create(
            hostname="worker1", state=Worker.STATE_ONLINE, health=Worker.HEALTH_ACTIVE
        )
        self.worker2 = Worker.objects.create(
            hostname="worker2",
            state=Worker.STATE_OFFLINE,
            health=Worker.HEALTH_MAINTENANCE,
        )

        # create devicetypes
        self.public_device_type1 = DeviceType.objects.create(name="public_device_type1")
        self.restricted_device_type1 = DeviceType.objects.create(
            name="restricted_device_type1"
        )
        GroupDeviceTypePermission.objects.assign_perm(
            DeviceType.VIEW_PERMISSION, self.group1, self.restricted_device_type1
        )
        GroupDeviceTypePermission.objects.assign_perm(
            DeviceType.CHANGE_PERMISSION, self.group1, self.restricted_device_type1
        )
        self.invisible_device_type1 = DeviceType.objects.create(
            name="invisible_device_type1", display=False
        )

        Alias.objects.create(name="test1", device_type=self.public_device_type1)
        Alias.objects.create(name="test2", device_type=self.public_device_type1)
        Alias.objects.create(name="test3", device_type=self.invisible_device_type1)
        Alias.objects.create(name="test4", device_type=self.restricted_device_type1)

        # create devices
        self.public_device1 = Device.objects.create(
            hostname="public01",
            device_type=self.public_device_type1,
            worker_host=self.worker1,
        )
        self.public_device2 = Device.objects.create(
            hostname="public02",
            device_type=self.public_device_type1,
            worker_host=self.worker1,
            state=Device.STATE_RUNNING,
        )
        self.retired_device1 = Device.objects.create(
            hostname="retired01",
            device_type=self.public_device_type1,
            health=Device.HEALTH_RETIRED,
            worker_host=self.worker2,
        )
        self.restricted_device1 = Device.objects.create(
            hostname="restricted_device1",
            device_type=self.restricted_device_type1,
            worker_host=self.worker1,
        )
        GroupDevicePermission.objects.assign_perm(
            Device.VIEW_PERMISSION, self.group1, self.restricted_device1
        )
        GroupDevicePermission.objects.assign_perm(
            Device.CHANGE_PERMISSION, self.group1, self.restricted_device1
        )

        # create testjobs
        self.public_testjob1 = TestJob.objects.create(
            definition=EXAMPLE_WORKING_JOB,
            submitter=self.user,
            requested_device_type=self.public_device_type1,
            health=TestJob.HEALTH_INCOMPLETE,
        )
        self.private_testjob1 = TestJob.objects.create(
            definition=EXAMPLE_JOB,
            submitter=self.admin,
            requested_device_type=self.public_device_type1,
            health=TestJob.HEALTH_COMPLETE,
        )
        self.private_testjob1.submit_time = timezone.now() - timedelta(days=7)
        self.private_testjob1.save()

        # create logs

        # create results for testjobs
        self.public_lava_suite = result_models.TestSuite.objects.create(
            name="lava", job=self.public_testjob1
        )
        self.public_test_case1 = result_models.TestCase.objects.create(
            name="foo",
            suite=self.public_lava_suite,
            result=result_models.TestCase.RESULT_FAIL,
        )
        self.public_test_case2 = result_models.TestCase.objects.create(
            name="bar",
            suite=self.public_lava_suite,
            result=result_models.TestCase.RESULT_PASS,
        )
        self.private_lava_suite = result_models.TestSuite.objects.create(
            name="lava", job=self.private_testjob1
        )
        self.private_test_case1 = result_models.TestCase.objects.create(
            name="foo",
            suite=self.private_lava_suite,
            result=result_models.TestCase.RESULT_FAIL,
        )
        self.private_test_case2 = result_models.TestCase.objects.create(
            name="bar",
            suite=self.private_lava_suite,
            result=result_models.TestCase.RESULT_PASS,
        )
        self.tag1 = Tag.objects.create(name="tag1", description="description1")
        self.tag2 = Tag.objects.create(name="tag2", description="description2")

    def hit(self, client, url):
        response = client.get(url)
        assert response.status_code == 200  # nosec - unit test support
        if hasattr(response, "content"):
            text = response.content.decode("utf-8")
            if response["Content-Type"] == "application/json":
                return json.loads(text)
            return text
        return ""

    def test_root(self):
        self.hit(self.userclient, reverse("api-root", args=[self.version]))

    def test_token(self):
        auth_dict = {
            "username": "%s" % self.user_no_token.get_username(),
            "password": self.user_no_token_pwd,
        }
        response = self.userclient_no_token.post(
            reverse("api-root", args=[self.version]) + "token/", auth_dict
        )
        assert response.status_code == 200  # nosec - unit test support
        text = response.content.decode("utf-8")
        assert "token" in json.loads(text).keys()  # nosec - unit test support

    def test_token_retrieval(self):
        auth_dict = {
            "username": "%s" % self.user.get_username(),
            "password": self.user_pwd,
        }
        response = self.userclient_no_token.post(
            reverse("api-root", args=[self.version]) + "token/", auth_dict
        )
        assert response.status_code == 200  # nosec - unit test support
        # response shouldn't cause exception. Below lines are just
        # additional check
        text = response.content.decode("utf-8")
        assert "token" in json.loads(text).keys()  # nosec - unit test support

    def test_testjobs(self):
        data = self.hit(
            self.userclient, reverse("api-root", args=[self.version]) + "jobs/"
        )
        assert len(data["results"]) == 1  # nosec - unit test support

    def test_testjobs_admin(self):
        data = self.hit(
            self.adminclient, reverse("api-root", args=[self.version]) + "jobs/"
        )
        assert len(data["results"]) == 2  # nosec - unit test support

    def test_testjob_item(self):
        self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "jobs/%s/" % self.public_testjob1.id,
        )

    def test_testjob_logs(self, monkeypatch, tmpdir):
        (tmpdir / "output.yaml").write_text(LOG_FILE, encoding="utf-8")
        monkeypatch.setattr(TestJob, "output_dir", str(tmpdir))

        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "jobs/%s/logs/" % self.public_testjob1.id,
        )

    def test_testjob_logs_offset(self, monkeypatch, tmpdir):
        (tmpdir / "output.yaml").write_text(LOG_FILE, encoding="utf-8")
        monkeypatch.setattr(TestJob, "output_dir", str(tmpdir))

        # use start=2 as log lines count start from 1
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "jobs/%s/logs/?start=2" % self.public_testjob1.id,
        )
        # the value below depends on the log fragment used
        # be careful when changing either the value below or the log fragment
        assert len(data) == 82  # nosec - unit test support

    def test_testjob_logs_offset_end(self, monkeypatch, tmpdir):
        (tmpdir / "output.yaml").write_text(LOG_FILE, encoding="utf-8")
        monkeypatch.setattr(TestJob, "output_dir", str(tmpdir))

        # use start=2 as log lines count start from 1
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "jobs/%s/logs/?start=1&end=2" % self.public_testjob1.id,
        )
        # the value below depends on the log fragment used
        # be careful when changing either the value below or the log fragment
        assert len(data) == 120  # nosec - unit test support

    def test_testjob_logs_bad_offset(self, monkeypatch, tmpdir):
        (tmpdir / "output.yaml").write_text(LOG_FILE, encoding="utf-8")
        monkeypatch.setattr(TestJob, "output_dir", str(tmpdir))

        # use start=2 as log lines count start from 1
        response = self.userclient.get(
            reverse("api-root", args=[self.version])
            + "jobs/%s/logs/?start=2&end=1" % self.public_testjob1.id
        )
        assert response.status_code == 404  # nosec - unit test support

    def test_testjob_nologs(self):
        response = self.userclient.get(
            reverse("api-root", args=[self.version])
            + "jobs/%s/logs/" % self.public_testjob1.id
        )
        assert response.status_code == 404  # nosec - unit test support

    def test_testjob_suites(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "jobs/%s/suites/" % self.public_testjob1.id,
        )
        assert len(data["results"]) == 1  # nosec - unit test support

    def test_testjob_tests(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "jobs/%s/tests/" % self.public_testjob1.id,
        )
        assert len(data["results"]) == 2  # nosec - unit test support

    def test_testjob_suite(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "jobs/%s/suites/%s/"
            % (self.public_testjob1.id, self.public_testjob1.testsuite_set.first().id),
        )
        assert (
            data["id"] == self.public_testjob1.testsuite_set.first().id
        )  # nosec - unit test support

    def test_testjob_suite_tests(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "jobs/%s/suites/%s/tests/"
            % (self.public_testjob1.id, self.public_testjob1.testsuite_set.first().id),
        )
        assert len(data["results"]) == 2  # nosec - unit test support

    def test_testjob_suite_test(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "jobs/%s/suites/%s/tests/%s/"
            % (
                self.public_testjob1.id,
                self.public_testjob1.testsuite_set.first().id,
                self.public_testjob1.testsuite_set.first().testcase_set.first().id,
            ),
        )
        assert (
            data["id"]
            == self.public_testjob1.testsuite_set.first().testcase_set.first().id
        )  # nosec - unit test support

    def test_testjob_metadata(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "jobs/%s/metadata/" % self.public_testjob1.id,
        )
        assert data["metadata"] == []  # nosec - unit test support

    def test_testjob_csv(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "jobs/%s/csv/" % self.public_testjob1.id,
        )
        csv_data = csv.reader(data.splitlines())
        assert list(csv_data)[1][0] == str(
            self.public_testjob1.id
        )  # nosec - unit test support

    def test_testjob_yaml(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "jobs/%s/yaml/" % self.public_testjob1.id,
        )
        data = yaml_load(data)
        assert data[0]["job"] == str(
            self.public_testjob1.id
        )  # nosec - unit test support

    def test_testjob_junit(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "jobs/%s/junit/" % self.public_testjob1.id,
        )
        tree = ET.fromstring(data)
        assert tree.tag == "testsuites"
        assert tree.attrib == {
            "failures": "0",
            "errors": "1",
            "tests": "2",
            "disabled": "0",
            "time": "0.0",
        }
        assert len(tree) == 1

        assert tree[0].tag == "testsuite"
        assert tree[0].attrib == {
            "name": "lava",
            "disabled": "0",
            "failures": "0",
            "errors": "1",
            "skipped": "0",
            "time": "0",
            "tests": "2",
        }
        assert len(tree[0]) == 2

        assert tree[0][0].tag == "testcase"
        assert tree[0][0].attrib["name"] == "foo"
        assert tree[0][0].attrib["classname"] == "lava"
        assert "timestamp" in tree[0][0].attrib
        assert len(tree[0][0]) == 1

        assert tree[0][0][0].tag == "error"
        assert tree[0][0][0].attrib == {"type": "error", "message": "failed"}
        assert len(tree[0][0][0]) == 0

        assert tree[0][1].tag == "testcase"
        assert tree[0][1].attrib["name"] == "bar"
        assert tree[0][1].attrib["classname"] == "lava"
        assert "timestamp" in tree[0][0].attrib
        assert len(tree[0][1]) == 0

    def test_testjob_tap13(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "jobs/%s/tap13/" % self.public_testjob1.id,
        )
        assert (  # nosec - unit test support
            data
            == """TAP version 13
1..2
# TAP results for lava
not ok 1 foo
ok 2 bar
"""
        )

    def test_testjob_suite_csv(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "jobs/%s/suites/%s/csv/"
            % (self.public_testjob1.id, self.public_testjob1.testsuite_set.first().id),
        )
        csv_data = csv.reader(data.splitlines())
        # Case id column is number 13 in a row.
        assert list(csv_data)[1][12] == str(
            self.public_testjob1.testsuite_set.first().testcase_set.first().id
        )  # nosec - unit test support

    def test_testjob_suite_yaml(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "jobs/%s/suites/%s/yaml/"
            % (self.public_testjob1.id, self.public_testjob1.testsuite_set.first().id),
        )
        data = yaml_load(data)
        assert (
            data[0]["suite"] == self.public_testjob1.testsuite_set.first().name
        )  # nosec - unit test support

    def test_testjob_validate(self):
        response = self.userclient.post(
            reverse("api-root", args=[self.version]) + "jobs/validate/",
            {"definition": EXAMPLE_WORKING_JOB},
        )
        assert response.status_code == 200  # nosec - unit test support
        msg = json.loads(response.content)
        assert msg["message"] == "Job valid."

    def test_testjobs_filters(self):
        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version]) + "jobs/?health=Incomplete",
        )
        assert len(data["results"]) == 1  # nosec - unit test support

        job1_submit_time = timezone.make_naive(self.public_testjob1.submit_time)
        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version])
            + "jobs/?submit_time=%s" % job1_submit_time,
        )
        assert len(data["results"]) == 1  # nosec - unit test support

        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version])
            + "jobs/?submit_time__lt=%s" % job1_submit_time,
        )
        assert len(data["results"]) == 1  # nosec - unit test support

        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version])
            + "jobs/?definition__contains=public_device",
        )
        assert len(data["results"]) == 1  # nosec - unit test support

    def test_devices_list(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version]) + "devices/?ordering=hostname",
        )
        assert len(data["results"]) == 2  # nosec - unit test support
        assert data["results"][0]["hostname"] == "public01"  # nosec - unit test support
        assert data["results"][1]["hostname"] == "public02"  # nosec - unit test support

    def test_devices_admin(self):
        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version]) + "devices/?ordering=hostname",
        )
        assert len(data["results"]) == 3  # nosec - unit test support
        assert data["results"][0]["hostname"] == "public01"  # nosec - unit test support
        assert data["results"][1]["hostname"] == "public02"  # nosec - unit test support
        assert (
            data["results"][2]["hostname"] == "restricted_device1"
        )  # nosec - unit test support

    def test_devices_retrieve(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version]) + "devices/public01/",
        )
        assert data["hostname"] == "public01"  # nosec - unit test support
        assert data["device_type"] == "public_device_type1"  # nosec - unit test support

    def test_devices_create_unauthorized(self):
        response = self.userclient.post(
            reverse("api-root", args=[self.version]) + "devices/",
            {
                "hostname": "public03",
                "device_type": "qemu",
                "health": "Idle",
                "pysical_owner": "admin",
            },
        )
        assert response.status_code == 403  # nosec - unit test support

    def test_devices_create(self):
        response = self.adminclient.post(
            reverse("api-root", args=[self.version]) + "devices/",
            {
                "hostname": "public03",
                "device_type": "public_device_type1",
                "health": "Good",
                "pysical_owner": "admin",
            },
        )
        assert response.status_code == 201  # nosec - unit test support

    def test_devices_delete_unauthorized(self):
        response = self.userclient.delete(
            reverse("api-root", args=[self.version]) + "devices/public02/"
        )
        assert response.status_code == 403  # nosec - unit test support

    def test_devices_delete(self):
        response = self.adminclient.delete(
            reverse("api-root", args=[self.version]) + "devices/public02/"
        )
        assert response.status_code == 204  # nosec - unit test support

    def test_devices_update_unauthorized(self):
        response = self.userclient.put(
            reverse("api-root", args=[self.version]) + "devices/public02/",
            {"device_type": "restricted_device_type1"},
        )
        assert response.status_code == 403  # nosec - unit test support

    def test_devices_update(self):
        response = self.adminclient.put(
            reverse("api-root", args=[self.version]) + "devices/public02/",
            {"device_type": "restricted_device_type1", "health": "Unknown"},
        )
        assert response.status_code == 200  # nosec - unit test support
        content = json.loads(response.content.decode("utf-8"))
        assert (
            content["device_type"] == "restricted_device_type1"
        )  # nosec - unit test support
        assert content["health"] == "Unknown"  # nosec - unit test support

    def test_devices_get_dictionary(self, monkeypatch, tmpdir):
        # invalid context
        response = self.userclient.get(
            reverse("api-root", args=[self.version])
            + "devices/public01/dictionary/?context={{"
        )
        assert response.status_code == 400  # nosec

        # no device dict
        monkeypatch.setattr(
            Device, "load_configuration", (lambda self, job_ctx, output_format: None)
        )
        response = self.userclient.get(
            reverse("api-root", args=[self.version]) + "devices/public01/dictionary/"
        )
        assert response.status_code == 400  # nosec

        # success
        monkeypatch.setattr(
            Device,
            "load_configuration",
            (lambda self, job_ctx, output_format: "device: dict"),
        )
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version]) + "devices/public01/dictionary/",
        )
        data = yaml_load(data)
        assert data == {"device": "dict"}  # nosec

    def test_devices_set_dictionary(self, monkeypatch, tmpdir):
        def save_configuration(self, data):
            assert data == "hello"  # nosec
            return True

        monkeypatch.setattr(Device, "save_configuration", save_configuration)

        response = self.adminclient.post(
            reverse("api-root", args=[self.version]) + "devices/public01/dictionary/",
            {"dictionary": "hello"},
        )
        assert response.status_code == 200  # nosec

        # No dictionary param.
        response = self.adminclient.post(
            reverse("api-root", args=[self.version]) + "devices/public01/dictionary/"
        )
        assert response.status_code == 400  # nosec

    def test_devices_filters(self):
        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version]) + "devices/?state=Idle",
        )
        assert len(data["results"]) == 2  # nosec - unit test support

        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version]) + "devices/?health=Retired",
        )
        assert len(data["results"]) == 0  # nosec - unit test support

        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version])
            + "devices/?all=True&health=Retired",
        )
        assert len(data["results"]) == 1  # nosec - unit test support

        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version])
            + "devices/?hostname__contains=public",
        )
        assert len(data["results"]) == 2  # nosec - unit test support

    def test_devicetypes(self):
        data = self.hit(
            self.userclient, reverse("api-root", args=[self.version]) + "devicetypes/"
        )
        assert len(data["results"]) == 2  # nosec - unit test support

    def test_devicetypes_admin(self):
        data = self.hit(
            self.adminclient, reverse("api-root", args=[self.version]) + "devicetypes/"
        )
        assert len(data["results"]) == 3  # nosec - unit test support

    def test_devicetype_view(self):
        response = self.userclient.get(
            reverse("api-root", args=[self.version])
            + "devicetypes/%s/" % self.public_device_type1.name
        )
        assert response.status_code == 200  # nosec - unit test support

    def test_devicetype_view_unauthorized(self):
        response = self.userclient.get(
            reverse("api-root", args=[self.version])
            + "devicetypes/%s/" % self.restricted_device_type1.name
        )
        assert response.status_code == 404  # nosec - unit test support

    def test_devicetype_view_admin(self):
        response = self.adminclient.get(
            reverse("api-root", args=[self.version])
            + "devicetypes/%s/" % self.restricted_device_type1.name
        )
        assert response.status_code == 200  # nosec - unit test support

    def test_devicetype_create_unauthorized(self):
        response = self.userclient.post(
            reverse("api-root", args=[self.version]) + "devicetypes/", {"name": "bbb"}
        )
        assert response.status_code == 403  # nosec - unit test support

    def test_devicetype_create_no_name(self):
        response = self.adminclient.post(
            reverse("api-root", args=[self.version]) + "devicetypes/", {}
        )
        assert response.status_code == 400  # nosec - unit test support

    def test_devicetype_create(self):
        response = self.adminclient.post(
            reverse("api-root", args=[self.version]) + "devicetypes/", {"name": "bbb"}
        )
        assert response.status_code == 201  # nosec - unit test support

    def test_devicetype_get_template(self, mocker, tmpdir):
        (tmpdir / "qemu.jinja2").write_text("hello", encoding="utf-8")
        mocker.patch("lava_server.files.File.KINDS", {"device-type": [str(tmpdir)]})

        # 1. normal case
        qemu_device_type1 = DeviceType.objects.create(name="qemu")
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "devicetypes/%s/template/" % qemu_device_type1.name,
        )
        data = yaml_load(data)
        assert data == str("hello")  # nosec

        # 2. Can't read the file
        bbb_device_type1 = DeviceType.objects.create(name="bbb")
        response = self.userclient.get(
            reverse("api-root", args=[self.version])
            + "devicetypes/%s/template/" % bbb_device_type1.name
        )
        assert response.status_code == 400  # nosec

    def test_devicetype_set_template(self, mocker, tmpdir):
        mocker.patch("lava_server.files.File.KINDS", {"device-type": [str(tmpdir)]})

        # 1. normal case
        qemu_device_type1 = DeviceType.objects.create(name="qemu")
        response = self.adminclient.post(
            reverse("api-root", args=[self.version])
            + "devicetypes/%s/template/" % qemu_device_type1.name,
            {"template": "hello world"},
        )
        assert response.status_code == 204  # nosec - unit test support
        assert (tmpdir / "qemu.jinja2").read_text(
            encoding="utf-8"
        ) == "hello world"  # nosec

    def test_devicetype_get_health_check(self, mocker, tmpdir):
        (tmpdir / "qemu.yaml").write_text("hello", encoding="utf-8")
        mocker.patch("lava_server.files.File.KINDS", {"health-check": [str(tmpdir)]})

        # 1. normal case
        qemu_device_type1 = DeviceType.objects.create(name="qemu")
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "devicetypes/%s/health_check/" % qemu_device_type1.name,
        )
        data = yaml_load(data)
        assert data == str("hello")  # nosec

        # 2. Can't read the health-check
        docker_device_type1 = DeviceType.objects.create(name="docker")
        response = self.userclient.get(
            reverse("api-root", args=[self.version])
            + "devicetypes/%s/health_check/" % docker_device_type1.name
        )
        assert response.status_code == 400  # nosec

    def test_devicetype_set_health_check(self, mocker, tmpdir):
        mocker.patch("lava_server.files.File.KINDS", {"health-check": [str(tmpdir)]})

        # 1. normal case
        qemu_device_type1 = DeviceType.objects.create(name="qemu")
        response = self.adminclient.post(
            reverse("api-root", args=[self.version])
            + "devicetypes/%s/health_check/" % qemu_device_type1.name,
            {"config": "hello world"},
        )
        assert response.status_code == 204  # nosec - unit test support
        assert (tmpdir / "qemu.yaml").read_text(
            encoding="utf-8"
        ) == "hello world"  # nosec

    def test_devicetype_filters(self):
        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version]) + "devicetypes/?display=True",
        )
        assert len(data["results"]) == 2  # nosec - unit test support

        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version])
            + "devicetypes/?display__in=True,False",
        )
        assert len(data["results"]) == 3  # nosec - unit test support

        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version])
            + "devicetypes/?name__contains=public",
        )
        assert len(data["results"]) == 1  # nosec - unit test support

        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version])
            + "devicetypes/?name__in=public_device_type1,restricted_device_type1",
        )
        assert len(data["results"]) == 2  # nosec - unit test support

    def test_workers_list(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version]) + "workers/?ordering=hostname",
        )
        # We get 3 workers because the default one (example.com) is always
        # created by the migrations
        assert len(data["results"]) == 3  # nosec - unit test support
        assert (  # nosec - unit test support
            data["results"][0]["hostname"] == "example.com"
        )
        assert data["results"][1]["hostname"] == "worker1"  # nosec - unit test support
        assert data["results"][1]["health"] == "Active"  # nosec - unit test support
        assert data["results"][1]["state"] == "Online"  # nosec - unit test support
        assert data["results"][2]["hostname"] == "worker2"  # nosec - unit test support
        assert (  # nosec - unit test support
            data["results"][2]["health"] == "Maintenance"
        )
        assert data["results"][2]["state"] == "Offline"  # nosec - unit test support

    def test_workers_retrieve(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "workers/%s/" % self.worker1.hostname,
        )
        assert data["hostname"] == "worker1"  # nosec - unit test support
        assert data["state"] == "Online"  # nosec - unit test support
        assert data["health"] == "Active"  # nosec - unit test support

    def test_workers_retrieve_with_dot(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version]) + "workers/example.com/",
        )
        assert data["hostname"] == "example.com"  # nosec - unit test support
        assert data["state"] == "Offline"  # nosec - unit test support
        assert data["health"] == "Active"  # nosec - unit test support

    def test_workers_create_unauthorized(self):
        response = self.userclient.post(
            reverse("api-root", args=[self.version]) + "workers/",
            {"hostname": "worker3", "description": "description3"},
        )
        assert response.status_code == 403  # nosec - unit test support

    def test_workers_create(self):
        response = self.adminclient.post(
            reverse("api-root", args=[self.version]) + "workers/",
            {"hostname": "worker3", "description": "description3"},
        )
        assert response.status_code == 201  # nosec - unit test support

    def test_workers_delete_unauthorized(self):
        response = self.userclient.delete(
            reverse("api-root", args=[self.version])
            + "workers/%s/" % self.worker2.hostname
        )
        assert response.status_code == 403  # nosec - unit test support

    def test_workers_delete(self):
        response = self.adminclient.delete(
            reverse("api-root", args=[self.version])
            + "workers/%s/" % self.worker2.hostname
        )
        assert response.status_code == 204  # nosec - unit test support

    def test_workers_get_env(self, monkeypatch, tmpdir):

        (tmpdir / "env.yaml").write_text("hello", encoding="utf-8")

        class MyPath(pathlib.PosixPath):
            def __new__(cls, path, *args, **kwargs):
                if path == "%s/env.yaml" % settings.GLOBAL_SETTINGS_PATH:
                    return super().__new__(
                        cls, str(tmpdir / "env.yaml"), *args, **kwargs
                    )
                elif path == "%s/worker1/env.yaml" % settings.DISPATCHER_CONFIG_PATH:
                    return super().__new__(
                        cls, str(tmpdir / "env.yaml"), *args, **kwargs
                    )
                else:
                    assert 0  # nosec

        monkeypatch.setattr(pathlib, "Path", MyPath)
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "workers/%s/env/" % self.worker1.hostname,
        )
        data = yaml_load(data)
        assert data == str("hello")  # nosec

        # worker does not exists
        response = self.userclient.get(
            reverse("api-root", args=[self.version]) + "workers/invalid_hostname/env/"
        )
        assert response.status_code == 404  # nosec

        # no configuration file
        (tmpdir / "env.yaml").remove()
        response = self.userclient.get(
            reverse("api-root", args=[self.version])
            + "workers/%s/env/" % self.worker1.hostname
        )
        assert response.status_code == 400  # nosec

    def test_workers_get_config(self, monkeypatch, tmpdir):
        (tmpdir / self.worker1.hostname).mkdir()
        (tmpdir / self.worker1.hostname / "dispatcher.yaml").write_text(
            "hello world", encoding="utf-8"
        )

        class MyPath(pathlib.PosixPath):
            def __new__(cls, path, *args, **kwargs):
                if (
                    path
                    == "%s/worker1/dispatcher.yaml" % settings.DISPATCHER_CONFIG_PATH
                ):
                    return super().__new__(
                        cls,
                        str(tmpdir / "worker1" / "dispatcher.yaml"),
                        *args,
                        **kwargs
                    )
                elif path == "%s/worker1.yaml" % settings.GLOBAL_SETTINGS_PATH:
                    return super().__new__(
                        cls, str(tmpdir / "worker1.yaml"), *args, **kwargs
                    )
                else:
                    assert 0  # nosec

        monkeypatch.setattr(pathlib, "Path", MyPath)
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "workers/%s/config/" % self.worker1.hostname,
        )
        data = yaml_load(data)
        assert data == str("hello world")  # nosec

        # worker does not exists
        response = self.userclient.get(
            reverse("api-root", args=[self.version])
            + "workers/invalid_hostname/config/"
        )
        assert response.status_code == 404  # nosec

        # no configuration file
        (tmpdir / self.worker1.hostname / "dispatcher.yaml").remove()
        (tmpdir / self.worker1.hostname).remove()
        response = self.userclient.get(
            reverse("api-root", args=[self.version])
            + "workers/%s/config/" % self.worker1.hostname
        )
        assert response.status_code == 400  # nosec

    def test_workers_set_env(self, monkeypatch, tmpdir):
        class MyPath(pathlib.PosixPath):
            def __new__(cls, path, *args, **kwargs):
                if path == "example.com":
                    return super().__new__(cls, path, *args, **kwargs)
                elif path == settings.DISPATCHER_CONFIG_PATH:
                    return super().__new__(cls, str(tmpdir), *args, **kwargs)
                else:
                    assert 0  # nosec

        monkeypatch.setattr(pathlib, "Path", MyPath)
        response = self.adminclient.post(
            reverse("api-root", args=[self.version])
            + "workers/%s/env/" % self.worker1.hostname,
            {"env": "hello"},
        )
        assert response.status_code == 200  # nosec
        assert (tmpdir / self.worker1.hostname / "env.yaml").read_text(  # nosec
            encoding="utf-8"
        ) == "hello"

        # worker does not exists
        response = self.adminclient.post(
            reverse("api-root", args=[self.version]) + "workers/invalid_hostname/env/",
            {"env": "hello"},
        )
        assert response.status_code == 404  # nosec

        # insufficient permissions
        response = self.userclient.post(
            reverse("api-root", args=[self.version])
            + "workers/%s/env/" % self.worker1.hostname,
            {"env": "hello"},
        )
        assert response.status_code == 403  # nosec

        # No env parameter
        response = self.adminclient.post(
            reverse("api-root", args=[self.version])
            + "workers/%s/env/" % self.worker1.hostname
        )
        assert response.status_code == 400  # nosec

    def test_workers_set_config(self, monkeypatch, tmpdir):
        class MyPath(pathlib.PosixPath):
            def __new__(cls, path, *args, **kwargs):
                if path == "example.com":
                    return super().__new__(cls, path, *args, **kwargs)
                elif path == settings.DISPATCHER_CONFIG_PATH:
                    return super().__new__(cls, str(tmpdir), *args, **kwargs)
                else:
                    assert 0  # nosec

        monkeypatch.setattr(pathlib, "Path", MyPath)
        response = self.adminclient.post(
            reverse("api-root", args=[self.version])
            + "workers/%s/config/" % self.worker1.hostname,
            {"config": "hello"},
        )
        assert response.status_code == 200  # nosec
        assert (tmpdir / self.worker1.hostname / "dispatcher.yaml").read_text(  # nosec
            encoding="utf-8"
        ) == "hello"

        # worker does not exists
        response = self.adminclient.post(
            reverse("api-root", args=[self.version])
            + "workers/invalid_hostname/config/",
            {"config": "hello"},
        )
        assert response.status_code == 404  # nosec

        # insufficient permissions
        response = self.userclient.post(
            reverse("api-root", args=[self.version])
            + "workers/%s/config/" % self.worker1.hostname,
            {"config": "hello"},
        )
        assert response.status_code == 403  # nosec

        # No config parameter
        response = self.adminclient.post(
            reverse("api-root", args=[self.version])
            + "workers/%s/config/" % self.worker1.hostname
        )
        assert response.status_code == 400  # nosec

    def test_workers_get_certificate(self, monkeypatch, tmpdir):
        (tmpdir / ("%s.key" % self.worker1.hostname)).write_text(
            "hello", encoding="utf-8"
        )

        class MyPath(pathlib.PosixPath):
            def __new__(cls, path, *args, **kwargs):
                if path == settings.SLAVES_CERTS:
                    return super().__new__(cls, str(tmpdir), *args, **kwargs)
                else:
                    assert 0  # nosec

        monkeypatch.setattr(pathlib, "Path", MyPath)

        # no permission
        response = self.userclient.get(
            reverse("api-root", args=[self.version])
            + "workers/%s/certificate/" % self.worker1.hostname
        )
        assert response.status_code == 403  # nosec

        GroupWorkerPermission.objects.assign_perm(
            Worker.CHANGE_PERMISSION, self.group2, self.worker1
        )
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version])
            + "workers/%s/certificate/" % self.worker1.hostname,
        )
        data = yaml_load(data)
        assert data == str("hello")  # nosec

        # worker does not exists
        response = self.userclient.get(
            reverse("api-root", args=[self.version])
            + "workers/invalid_hostname/certificate/"
        )
        assert response.status_code == 404  # nosec

        # no encryption key file
        (tmpdir / ("%s.key" % self.worker1.hostname)).remove()
        response = self.userclient.get(
            reverse("api-root", args=[self.version])
            + "workers/%s/certificate/" % self.worker1.hostname
        )
        assert response.status_code == 400  # nosec

    def test_workers_set_certificate(self, monkeypatch, tmpdir):
        class MyPath(pathlib.PosixPath):
            def __new__(cls, path, *args, **kwargs):
                if path == settings.SLAVES_CERTS:
                    return super().__new__(cls, str(tmpdir), *args, **kwargs)
                else:
                    assert 0  # nosec

        monkeypatch.setattr(pathlib, "Path", MyPath)
        response = self.adminclient.post(
            reverse("api-root", args=[self.version])
            + "workers/%s/certificate/" % self.worker1.hostname,
            {"key": "hello"},
        )
        assert response.status_code == 200  # nosec
        assert (tmpdir / ("%s.key" % self.worker1.hostname)).read_text(  # nosec
            encoding="utf-8"
        ) == "hello"

        # worker does not exists
        response = self.adminclient.post(
            reverse("api-root", args=[self.version])
            + "workers/invalid_hostname/certificate/",
            {"env": "hello"},
        )
        assert response.status_code == 404  # nosec

        # insufficient permissions
        response = self.userclient.post(
            reverse("api-root", args=[self.version])
            + "workers/%s/certificate/" % self.worker1.hostname,
            {"env": "hello"},
        )
        assert response.status_code == 403  # nosec

        # No env parameter
        response = self.adminclient.post(
            reverse("api-root", args=[self.version])
            + "workers/%s/certificate/" % self.worker1.hostname
        )
        assert response.status_code == 400  # nosec

    def test_workers_filters(self):
        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version]) + "workers/?health=Active",
        )
        assert len(data["results"]) == 2  # nosec - unit test support

        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version]) + "workers/?state=Online",
        )
        assert len(data["results"]) == 1  # nosec - unit test support

        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version]) + "workers/?hostname=worker1",
        )
        assert len(data["results"]) == 1  # nosec - unit test support

    def test_aliases_list(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version]) + "aliases/?ordering=name",
        )
        # We have 4 aliases, but only 2 are visible
        assert len(data["results"]) == 2  # nosec - unit test support
        assert data["results"][0]["name"] == "test1"  # nosec - unit test support
        assert data["results"][1]["name"] == "test2"  # nosec - unit test support

    def test_aliases_retrieve(self):
        data = self.hit(
            self.userclient, reverse("api-root", args=[self.version]) + "aliases/test1/"
        )
        assert data["name"] == "test1"  # nosec - unit test support
        assert data["device_type"] == "public_device_type1"  # nosec - unit test support

    def test_aliases_create_unauthorized(self):
        response = self.userclient.post(
            reverse("api-root", args=[self.version]) + "aliases/",
            {"name": "test5", "device_type": "qemu"},
        )
        assert response.status_code == 403  # nosec - unit test support

    def test_aliases_create(self):
        response = self.adminclient.post(
            reverse("api-root", args=[self.version]) + "aliases/",
            {"name": "test5", "device_type": "public_device_type1"},
        )
        assert response.status_code == 201  # nosec - unit test support

    def test_aliases_delete_unauthorized(self):
        response = self.userclient.delete(
            reverse("api-root", args=[self.version]) + "aliases/test2/"
        )
        assert response.status_code == 403  # nosec - unit test support

    def test_aliases_delete(self):
        response = self.adminclient.delete(
            reverse("api-root", args=[self.version]) + "aliases/test2/"
        )
        assert response.status_code == 204  # nosec - unit test support

    def test_aliases_filters(self):
        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version]) + "aliases/?name=test2",
        )
        assert len(data["results"]) == 1  # nosec - unit test support

        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version]) + "aliases/?name__in=test1,test2",
        )
        assert len(data["results"]) == 2  # nosec - unit test support

        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version]) + "aliases/?name__contains=2",
        )
        assert len(data["results"]) == 1  # nosec - unit test support

    def test_submit_unauthorized(self):
        response = self.userclient.post(
            reverse("api-root", args=[self.version]) + "jobs/",
            {"definition": EXAMPLE_JOB},
        )
        assert response.status_code == 400  # nosec - unit test support

    def test_submit_bad_request_no_device_type(self):
        response = self.adminclient.post(
            reverse("api-root", args=[self.version]) + "jobs/",
            {"definition": EXAMPLE_JOB},
            format="json",
        )
        assert response.status_code == 400  # nosec - unit test support
        content = json.loads(response.content.decode("utf-8"))
        assert (
            content["message"] == "job submission failed: 'device_type'."
        )  # nosec - unit test support

    def test_submit(self):
        response = self.adminclient.post(
            reverse("api-root", args=[self.version]) + "jobs/",
            {"definition": EXAMPLE_WORKING_JOB},
            format="json",
        )
        assert response.status_code == 201  # nosec - unit test support
        assert TestJob.objects.count() == 3  # nosec - unit test support

    def test_resubmit_unauthorized(self):
        response = self.userclient.post(
            reverse("api-root", args=[self.version])
            + "jobs/%s/resubmit/" % self.private_testjob1.id
        )
        assert response.status_code == 404  # nosec - unit test support

    def test_resubmit(self):
        response = self.adminclient.post(
            reverse("api-root", args=[self.version])
            + "jobs/%s/resubmit/" % self.public_testjob1.id
        )
        assert response.status_code == 201  # nosec - unit test support
        assert TestJob.objects.count() == 3  # nosec - unit test support

    def test_cancel(self, mocker):
        mocker.patch("lava_scheduler_app.models.TestJob.cancel")
        response = self.adminclient.get(
            reverse("api-root", args=[self.version])
            + "jobs/%s/cancel/" % self.public_testjob1.id
        )
        assert response.status_code == 200  # nosec - unit test support
        msg = json.loads(response.content)
        assert msg["message"] == "Job cancel signal sent."  # nosec - unit test support

    def test_tags_list(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version]) + "tags/?ordering=name",
        )
        assert len(data["results"]) == 2  # nosec - unit test support
        assert data["results"][0]["name"] == "tag1"  # nosec - unit test support
        assert data["results"][1]["name"] == "tag2"  # nosec - unit test support

    def test_tags_retrieve(self):
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version]) + "tags/%s/" % self.tag1.id,
        )
        assert data["name"] == "tag1"  # nosec - unit test support
        assert data["description"] == "description1"  # nosec - unit test support

    def test_tags_create_unauthorized(self):
        response = self.userclient.post(
            reverse("api-root", args=[self.version]) + "tags/",
            {"name": "tag3", "description": "description3"},
        )
        assert response.status_code == 403  # nosec - unit test support

    def test_tags_create(self):
        response = self.adminclient.post(
            reverse("api-root", args=[self.version]) + "tags/",
            {"name": "tag3", "description": "description3"},
        )
        assert response.status_code == 201  # nosec - unit test support

    def test_tags_delete_unauthorized(self):
        response = self.userclient.delete(
            reverse("api-root", args=[self.version]) + "tags/%s/" % self.tag2.id
        )
        assert response.status_code == 403  # nosec - unit test support

    def test_tags_delete(self):
        response = self.adminclient.delete(
            reverse("api-root", args=[self.version]) + "tags/%s/" % self.tag2.id
        )
        assert response.status_code == 204  # nosec - unit test support

    def test_devicetype_permissions_list_unauthorized(self):
        response = self.userclient.get(
            reverse("api-root", args=[self.version]) + "permissions/devicetypes/"
        )
        assert response.status_code == 403  # nosec - unit test support

    def test_devicetype_permissions_list(self):
        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version])
            + "permissions/devicetypes/?ordering=id",
        )
        assert len(data["results"]) == 2  # nosec - unit test support
        assert (
            data["results"][0]["devicetype"] == "restricted_device_type1"
        )  # nosec - unit test support

    def test_devicetype_permissions_retrieve_unauthorized(self):
        response = self.userclient.get(
            reverse("api-root", args=[self.version]) + "permissions/devicetypes/1/"
        )
        assert response.status_code == 403  # nosec - unit test support

    def test_devicetype_permissions_retrieve(self):
        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version])
            + "permissions/devicetypes/%s/"
            % GroupDeviceTypePermission.objects.first().id,
        )
        assert (
            data["id"] == GroupDeviceTypePermission.objects.first().id
        )  # nosec - unit test support
        assert (
            data["devicetype"] == "restricted_device_type1"
        )  # nosec - unit test support

    def test_devicetype_permissions_create(self):
        response = self.adminclient.post(
            reverse("api-root", args=[self.version]) + "permissions/devicetypes/",
            {
                "group": "group1",
                "devicetype": "public_device_type1",
                "permission": "view_devicetype",
            },
        )
        assert response.status_code == 201  # nosec - unit test support

    def test_devicetype_permissions_delete_unauthorized(self):
        response = self.userclient.delete(
            reverse("api-root", args=[self.version])
            + "permissions/devicetypes/%s/"
            % GroupDeviceTypePermission.objects.first().id
        )
        assert response.status_code == 403  # nosec - unit test support

    def test_devicetype_permissions_delete(self):
        response = self.adminclient.delete(
            reverse("api-root", args=[self.version])
            + "permissions/devicetypes/%s/"
            % GroupDeviceTypePermission.objects.first().id
        )
        assert response.status_code == 204  # nosec - unit test support

    def test_device_permissions_list_unauthorized(self):
        response = self.userclient.get(
            reverse("api-root", args=[self.version]) + "permissions/devices/"
        )
        assert response.status_code == 403  # nosec - unit test support

    def test_device_permissions_list(self):
        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version])
            + "permissions/devices/?ordering=id",
        )
        assert len(data["results"]) == 2  # nosec - unit test support
        assert (
            data["results"][0]["device"] == "restricted_device1"
        )  # nosec - unit test support

    def test_device_permissions_retrieve_unauthorized(self):
        response = self.userclient.get(
            reverse("api-root", args=[self.version]) + "permissions/devices/1/"
        )
        assert response.status_code == 403  # nosec - unit test support

    def test_device_permissions_retrieve(self):
        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version])
            + "permissions/devices/%s/" % GroupDevicePermission.objects.first().id,
        )
        assert (
            data["id"] == GroupDevicePermission.objects.first().id
        )  # nosec - unit test support
        assert data["device"] == "restricted_device1"  # nosec - unit test support

    def test_device_permissions_create(self):
        response = self.adminclient.post(
            reverse("api-root", args=[self.version]) + "permissions/devices/",
            {"group": "group1", "device": "public01", "permission": "view_device"},
        )
        assert response.status_code == 201  # nosec - unit test support

    def test_device_permissions_delete_unauthorized(self):
        response = self.userclient.delete(
            reverse("api-root", args=[self.version])
            + "permissions/devices/%s/" % GroupDevicePermission.objects.first().id
        )
        assert response.status_code == 403  # nosec - unit test support

    def test_device_permissions_delete(self):
        response = self.adminclient.delete(
            reverse("api-root", args=[self.version])
            + "permissions/devices/%s/" % GroupDevicePermission.objects.first().id
        )
        assert response.status_code == 204  # nosec - unit test support

    def test_tags_filters(self):
        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version]) + "tags/?name=tag1",
        )
        assert len(data["results"]) == 1  # nosec - unit test support

        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version]) + "tags/?name__in=tag1,tag2",
        )
        assert len(data["results"]) == 2  # nosec - unit test support

        data = self.hit(
            self.adminclient,
            reverse("api-root", args=[self.version]) + "tags/?name__contains=2",
        )
        assert len(data["results"]) == 1  # nosec - unit test support

    def test_system_certificate(self, monkeypatch, tmpdir):
        (tmpdir / "master.key").write_text("hello", encoding="utf-8")

        class MyPath(pathlib.PosixPath):
            def __new__(cls, path, *args, **kwargs):
                if path == settings.MASTER_CERT_PUB:
                    return super().__new__(
                        cls, str(tmpdir / "master.key"), *args, **kwargs
                    )
                else:
                    assert 0  # nosec

        monkeypatch.setattr(pathlib, "Path", MyPath)
        data = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version]) + "system/certificate/",
        )
        data = yaml_load(data)
        assert data == str("hello")  # nosec

        # no encryption key file
        (tmpdir / "master.key").remove()
        response = self.userclient.get(
            reverse("api-root", args=[self.version]) + "system/certificate/"
        )
        assert response.status_code == 404  # nosec

    def test_system_version(self):
        version = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version]) + "system/version/",
        )
        assert version["version"] == __version__
        response = self.userclient_no_token.get(
            reverse("api-root", args=[self.version]) + "system/version/"
        )
        assert response.status_code == 403  # nosec

    def test_system_whoami(self):
        response = self.hit(
            self.userclient, reverse("api-root", args=[self.version]) + "system/whoami/"
        )
        assert response["user"] == self.user.username
        response = self.userclient_no_token.get(
            reverse("api-root", args=[self.version]) + "system/whoami/"
        )
        assert response.status_code == 403  # nosec

    def test_system_master_config(self):
        response = self.hit(
            self.userclient,
            reverse("api-root", args=[self.version]) + "system/master_config/",
        )
        assert response["EVENT_SOCKET"] == settings.EVENT_SOCKET
        assert response["EVENT_TOPIC"] == settings.EVENT_TOPIC
        assert response["EVENT_NOTIFICATION"] == settings.EVENT_NOTIFICATION
        assert response["LOG_SIZE_LIMIT"] == settings.LOG_SIZE_LIMIT

        response = self.userclient_no_token.get(
            reverse("api-root", args=[self.version]) + "system/master_config/"
        )
        assert response.status_code == 403  # nosec


def test_view_root(client):
    ret = client.get(reverse("api-root", args=[versions.versions[-1]]) + "?format=api")
    assert ret.status_code == 200


def test_view_devices(client, db):
    ret = client.get(
        reverse("api-root", args=[versions.versions[-1]]) + "devices/?format=api"
    )
    assert ret.status_code == 200


def test_view_devicetypes(client, db):
    ret = client.get(
        reverse("api-root", args=[versions.versions[-1]]) + "devicetypes/?format=api"
    )
    assert ret.status_code == 200


def test_view_jobs(client, db):
    ret = client.get(
        reverse("api-root", args=[versions.versions[-1]]) + "jobs/?format=api"
    )
    assert ret.status_code == 200


def test_view_workers(client, db):
    ret = client.get(
        reverse("api-root", args=[versions.versions[-1]]) + "workers/?format=api"
    )
    assert ret.status_code == 200
