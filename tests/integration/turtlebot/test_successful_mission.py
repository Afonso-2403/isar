import json
import shutil
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import numpy as np
import pytest
from alitra import Frame, Position
from fastapi import FastAPI
from injector import Injector
from starlette.testclient import TestClient

from isar.apis.api import API
from isar.config.settings import settings
from isar.modules import (
    APIModule,
    EventsModule,
    LocalPlannerModule,
    LocalStorageModule,
    RequestHandlerModule,
    RobotModule,
    SchedulingUtilitiesModule,
    ServiceModule,
    SharedStateModule,
    StateMachineModule,
)
from isar.state_machine.states_enum import States
from robot_interface.models.mission.mission import Mission
from robot_interface.models.mission.task import ReturnToHome
from tests.isar.state_machine.test_state_machine import (
    StateMachineThread,
    UploaderThread,
)
from tests.test_modules import MockMqttModule, MockNoAuthenticationModule


@pytest.fixture()
def injector_turtlebot():
    settings.ROBOT_PACKAGE = "isar_turtlebot"
    settings.LOCAL_STORAGE_PATH = "./tests/results"
    settings.PREDEFINED_MISSIONS_FOLDER = (
        "./tests/integration/turtlebot/config/missions"
    )
    settings.MAPS_FOLDER = "tests/integration/turtlebot/config/maps"
    settings.DEFAULT_MAP = "turtleworld"

    return Injector(
        [
            APIModule,
            MockNoAuthenticationModule,
            EventsModule,
            SharedStateModule,
            RequestHandlerModule,
            RobotModule,
            ServiceModule,
            StateMachineModule,
            LocalPlannerModule,
            LocalStorageModule,
            SchedulingUtilitiesModule,
            MockMqttModule,
        ]
    )


@pytest.fixture()
def state_machine_thread(injector_turtlebot) -> StateMachineThread:
    return StateMachineThread(injector=injector_turtlebot)


@pytest.fixture()
def uploader_thread(injector_turtlebot) -> UploaderThread:
    return UploaderThread(injector=injector_turtlebot)


@pytest.fixture(autouse=True)
def run_before_and_after_tests() -> None:  # type: ignore
    results_folder: Path = Path("tests/results")
    yield

    print("Removing temporary results folder for testing")
    if results_folder.exists():
        shutil.rmtree(results_folder)
    print("Cleanup finished")


def test_successful_mission(
    injector_turtlebot, state_machine_thread, uploader_thread, access_token
) -> None:
    integration_test_timeout: timedelta = timedelta(minutes=5)
    app: FastAPI = injector_turtlebot.get(API).get_app()
    client: TestClient = TestClient(app=app)

    _id: int = 2

    _ = client.post(
        f"schedule/start-mission?ID={_id}",
        headers={"Authorization": "Bearer {}".format(access_token)},
    )
    time.sleep(5)

    mission_id: str = state_machine_thread.state_machine.current_mission.id
    mission: Mission = deepcopy(state_machine_thread.state_machine.current_mission)

    start_time: datetime = datetime.now(timezone.utc)
    while state_machine_thread.state_machine.current_state != States.Idle:
        if (datetime.now(timezone.utc) - start_time) > integration_test_timeout:
            raise TimeoutError
        time.sleep(5)

    mission_result_folder: Path = Path(f"tests/results/{mission_id}")

    drive_to_tasks: List[ReturnToHome] = [
        task for task in mission.tasks if isinstance(task, ReturnToHome)
    ]

    expected_positions: list = [task.pose for task in drive_to_tasks]

    paths = mission_result_folder.rglob("*.json")
    for path in paths:
        with open(path) as json_file:
            metadata = json.load(json_file)
        files_metadata: dict = metadata["data"][0]["files"][0]
        filename: str = files_metadata["file_name"]
        inspection_file: Path = mission_result_folder.joinpath(filename)

        actual_position: Position = Position(
            x=files_metadata["x"],
            y=files_metadata["y"],
            z=files_metadata["z"],
            frame=Frame("asset"),
        )

        close_to_expected_positions: List[bool] = []
        for expected_position in expected_positions:
            close_to_expected_positions.append(
                np.allclose(
                    actual_position.to_list(), expected_position.to_list(), atol=0.2
                )
            )

        assert any(close_to_expected_positions)
        assert inspection_file.exists()
    assert (
        len(list(mission_result_folder.glob("*"))) == 6
    )  # Expected two images, one thermal + three metadata files
