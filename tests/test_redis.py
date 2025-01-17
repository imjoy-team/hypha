import asyncio
import time

import numpy as np
import pytest
import pytest_asyncio
from imjoy_rpc.hypha.websocket_client import connect_to_server

from hypha.core.store import RedisStore

from . import SIO_PORT, find_item

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def redis_store():
    """Represent the redis store."""
    store = RedisStore(None, redis_uri="/tmp/redis-temp.db", redis_port=6388)
    await store.init(reset_redis=True)
    yield store
    store.teardown()


async def test_redis_store(redis_store):
    """Test the redis store."""
    # Test adding a workspace
    await redis_store.register_workspace(
        dict(
            name="test",
            owners=[],
            visibility="protected",
            persistent=True,
            read_only=False,
        ),
        overwrite=True,
    )
    assert "test" in await redis_store.list_all_workspaces()

    api = await redis_store.connect_to_workspace("test", client_id="test-plugin-99")
    assert set(await api.list_clients()) == {"test-plugin-99", "workspace-manager"}
    await api.log("hello")
    services = await api.list_services()
    assert len(services) == 1
    assert await api.generate_token()
    ws = await api.create_workspace(
        dict(
            name="test-2",
            owners=[],
            visibility="protected",
            persistent=True,
            read_only=False,
        ),
        overwrite=True,
    )
    assert ws["name"] == "test-2"
    # assert await ws.list_clients() == ["workspace-manager"]
    ws2 = await api.get_workspace_info("test-2")
    assert ws2["name"] == "test-2"

    def echo(data):
        return data

    interface = {
        "id": "test-service",
        "name": "my service",
        "config": {},
        "setup": print,
        "echo": echo,
    }
    await api.register_service(interface)

    wm = await redis_store.connect_to_workspace("test", client_id="test-plugin-22")
    clients = await wm.list_clients()
    assert set(clients) == set(
        ["workspace-manager", "test-plugin-22", "test-plugin-99"]
    )
    rpc = wm.rpc
    services = await wm.list_services()
    service = await rpc.get_remote_service("test-plugin-99:test-service")

    assert callable(service.echo)
    assert await service.echo("hello") == "hello"
    assert await service.echo("hello") == "hello"


async def test_websocket_server(fastapi_server, test_user_token):
    """Test the websocket server."""
    wm = await connect_to_server(
        dict(
            server_url=f"ws://127.0.0.1:{SIO_PORT}/ws",
            client_id="test-plugin-1",
            token=test_user_token,
        )
    )
    await wm.log("hello")
    assert len(await wm.list_user_clients()) == 1

    assert set(await wm.list_clients()) == {
        "test-plugin-1",
        "workspace-manager",
    }
    rpc = wm.rpc

    def echo(data):
        return data

    await rpc.register_service(
        {
            "id": "test-service",
            "name": "my service",
            "config": {"visibility": "public"},
            "setup": print,
            "echo": echo,
            "square": lambda x: x ** 2,
        }
    )

    # Relative service means from the same client
    assert await rpc.get_remote_service("test-service")

    await rpc.register_service(
        {
            "id": "default",
            "name": "my service",
            "config": {},
            "setup": print,
            "echo": echo,
        }
    )

    svc = await rpc.get_remote_service("test-plugin-1:test-service")

    assert await svc.echo("hello") == "hello"

    services = await wm.list_services()
    assert (
        len(services) == 2
    )  # built-in and workspace-manager.default service are not listed

    svc = await wm.get_service("test-plugin-1:test-service")
    assert await svc.echo("hello") == "hello"

    # Get public service from another workspace
    wm3 = await connect_to_server({"server_url": f"ws://127.0.0.1:{SIO_PORT}/ws"})
    rpc3 = wm3.rpc
    svc7 = await rpc3.get_remote_service(
        f"{wm.config.workspace}/test-plugin-1:test-service"
    )
    svc7 = await wm3.get_service(f"{wm.config.workspace}/test-plugin-1:test-service")
    assert await svc7.square(9) == 81

    # Change the service to protected
    await rpc.register_service(
        {
            "id": "test-service",
            "name": "my service",
            "config": {"visibility": "protected"},
            "setup": print,
            "echo": echo,
            "square": lambda x: x ** 2,
        },
        overwrite=True,
    )

    # It should fail due to permission error
    with pytest.raises(
        Exception, match=r".*Permission denied for service: test-service.*"
    ):
        await rpc3.get_remote_service(
            f"{wm.config.workspace}/test-plugin-1:test-service"
        )

    # Should fail if we try to bypass the get_remote_service call
    remote_echo = rpc3._generate_remote_method(
        {
            "_rtarget": f"{wm.config.workspace}/test-plugin-1",
            "_rmethod": "services.test-service.echo",
            "_rpromise": True,
        }
    )
    with pytest.raises(Exception, match=r".*Permission denied for protected method.*"):
        await remote_echo(123)

    wm2 = await connect_to_server(
        dict(
            server_url=f"ws://127.0.0.1:{SIO_PORT}/ws",
            workspace=wm.config.workspace,
            client_id="test-plugin-6",
            token=test_user_token,
            method_timeout=3,
        )
    )
    rpc2 = wm2.rpc

    svc2 = await rpc2.get_remote_service(
        f"{wm.config.workspace}/test-plugin-1:test-service"
    )
    assert await svc2.echo("hello") == "hello"
    assert len(rpc2._object_store) == 1
    svc3 = await svc2.echo(svc2)
    assert len(rpc2._object_store) == 1
    assert await svc3.echo("hello") == "hello"

    svc4 = await svc2.echo(
        {
            "add_one": lambda x: x + 1,
        }
    )
    assert len(rpc2._object_store) > 0

    # It should fail because add_one is not a service and will be destroyed after the session
    with pytest.raises(Exception, match=r".*Method not found:.*"):
        assert await svc4.add_one(99) == 100

    svc5_info = await rpc2.register_service(
        {
            "id": "default",
            "add_one": lambda x: x + 1,
            "inner": {"square": lambda y: y ** 2},
        }
    )
    svc5 = await rpc2.get_remote_service(svc5_info["id"])
    svc6 = await svc2.echo(svc5)
    assert await svc6.add_one(99) == 100
    assert await svc6.inner.square(10) == 100
    array = np.zeros([2048, 1000, 1])
    array2 = await svc6.add_one(array)
    np.testing.assert_array_equal(array2, array + 1)

    assert len(await wm2.list_user_clients()) == 2
    await wm.disconnect()
    await asyncio.sleep(0.5)
    assert len(await wm2.list_user_clients()) == 1

    with pytest.raises(Exception, match=r".*Service already exists: default.*"):
        await rpc2.register_service(
            {
                "id": "default",
                "add_two": lambda x: x + 2,
            }
        )

    await rpc2.unregister_service("default")
    await rpc2.register_service(
        {
            "id": "default",
            "add_two": lambda x: x + 2,
        }
    )

    await rpc2.register_service({"id": "add-two", "blocking_sleep": time.sleep})
    svc5 = await rpc2.get_remote_service(f"{wm.config.workspace}/test-plugin-6:add-two")
    # This will fail because the service is blocking
    with pytest.raises(Exception, match=r".*Method call time out:.*"):
        await svc5.blocking_sleep(4)

    await svc5.blocking_sleep(0.5)

    await rpc2.register_service(
        {
            "id": "executor-test",
            "config": {"run_in_executor": True, "visibility": "public"},
            "blocking_sleep": time.sleep,
        }
    )
    svc5 = await rpc2.get_remote_service(
        f"{wm.config.workspace}/test-plugin-6:executor-test"
    )
    # This should be fine because it is run in executor
    await svc5.blocking_sleep(3)
    summary = await wm2.get_summary()
    assert summary["client_count"] == 2
    assert summary["service_count"] == 3
    assert find_item(summary["services"], "name", "executor-test")
    await wm2.disconnect()
