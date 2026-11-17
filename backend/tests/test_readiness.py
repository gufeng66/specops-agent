"""checkpoint 模式与 readiness：sqlite 失败必须启动失败；memory 显式降级；依赖失效 503。"""

import json

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient

import app.main as main_mod
from app.config import settings
from app.main import app
from app.memory import checkpoint as cp


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    cp.reset_checkpointer()
    monkeypatch.setattr(settings, "checkpoint_mode", "sqlite")
    yield
    cp.reset_checkpointer()


@pytest.mark.asyncio
async def test_sqlite_failure_raises_setup_error(tmp_path, monkeypatch):
    # 指向一个目录，SQLite 无法把目录当库文件打开。
    blocker = tmp_path / "not-a-db"
    blocker.mkdir()
    monkeypatch.setattr(settings, "checkpoint_path", str(blocker))

    with pytest.raises(cp.CheckpointSetupError):
        await cp.setup_checkpointer()

    status = cp.checkpointer_status()
    assert status["ready"] is False
    assert status["degraded"] is False


@pytest.mark.asyncio
async def test_memory_mode_is_explicitly_degraded(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "checkpoint_mode", "memory")
    await cp.setup_checkpointer()

    status = cp.checkpointer_status()
    assert status["ready"] is True
    assert status["degraded"] is True
    assert status["mode"] == "memory"
    assert status["backend"] == "memory"


@pytest.mark.asyncio
async def test_get_checkpointer_raises_before_setup():
    cp.reset_checkpointer()
    with pytest.raises(cp.CheckpointNotReadyError):
        cp.get_checkpointer()


@pytest.mark.asyncio
async def test_ready_reports_degraded_memory_mode(monkeypatch):
    monkeypatch.setattr(settings, "checkpoint_mode", "memory")
    await cp.setup_checkpointer()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/ready")
        health = await ac.get("/health")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    body = resp.json()
    checkpoint = next(c for c in body["checks"] if c["name"] == "checkpoint")
    assert checkpoint["ok"] is True
    assert checkpoint["degraded"] is True
    assert body["degraded"] is True
    assert body["checkpoint_mode"] == "memory"


@pytest.mark.asyncio
async def test_ready_returns_503_when_ping_fails(monkeypatch):
    """readiness 用 ping() 探测业务库；探测失败返回 503，不再依赖 list_running()。"""
    monkeypatch.setattr(settings, "checkpoint_mode", "memory")
    await cp.setup_checkpointer()

    async def failing_ping():
        return False

    monkeypatch.setattr("app.main.memory_store.ping", failing_ping)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    business = next(c for c in body["checks"] if c["name"] == "business_db")
    assert business["ok"] is False
    assert "ping" in business["detail"]


@pytest.mark.asyncio
async def test_ping_succeeds_on_openable_db(tmp_path):
    """可打开的业务库 ping 返回 True。"""
    from app.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "ping.db"))
    await store.init()
    assert await store.ping() is True


@pytest.mark.asyncio
async def test_ping_returns_false_on_unreadable_path(tmp_path):
    """SQLite 文件不可读（这里是目录）时 ping 返回 False，而不是抛异常。"""
    from app.memory.store import MemoryStore

    blocker = tmp_path / "not-a-db"
    blocker.mkdir()
    store = MemoryStore(str(blocker))
    assert await store.ping() is False


@pytest.mark.asyncio
async def test_schema_status_isolates_one_corrupt_snapshot(tmp_path):
    from app.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "corrupt.db"))
    await store.init()
    snap = await store.ensure_thread("t-corrupt")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE threads SET snapshot_json = ? WHERE thread_id = ?",
            ("{not-json", snap.thread_id),
        )
        await db.commit()

    status = await store.schema_status()
    assert status["ok"] is True
    assert status["degraded"] is True
    assert status["corrupt_count"] == 1
    assert status["corrupt_thread_ids"] == ["t-corrupt"]


@pytest.mark.asyncio
async def test_ready_returns_503_for_future_snapshot_schema(tmp_path, monkeypatch):
    from app.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "future.db"))
    await store.init()
    snap = await store.ensure_thread("t-future")
    raw = snap.model_dump(mode="json")
    raw["schema_version"] = 99
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE threads SET snapshot_json = ? WHERE thread_id = ?",
            (json.dumps(raw, ensure_ascii=False), snap.thread_id),
        )
        await db.commit()

    monkeypatch.setattr(main_mod, "memory_store", store)
    response = await main_mod.ready()
    assert response.status_code == 503
    body = json.loads(response.body)
    check = next(row for row in body["checks"] if row["name"] == "store_schema")
    assert check["ok"] is False
    assert check["future_count"] == 1


@pytest.mark.asyncio
async def test_list_running_failure_does_not_change_ping_semantics(tmp_path, monkeypatch):
    """list_running() 查询错误不再承担 readiness 语义；ping() 独立成功即可。"""
    from app.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "ready.db"))
    await store.init()

    async def broken_list_running():
        raise RuntimeError("query broken")

    monkeypatch.setattr(store, "list_running", broken_list_running)
    # ping 只做 SELECT 1，不受业务查询影响。
    assert await store.ping() is True


@pytest.mark.asyncio
async def test_sqlite_empty_path_refuses_startup(monkeypatch):
    """sqlite 模式下空 CHECKPOINT_PATH 是配置错误，必须启动失败而不是隐式内存降级。"""
    monkeypatch.setattr(settings, "checkpoint_path", "   ")

    with pytest.raises(cp.CheckpointSetupError) as excinfo:
        await cp.setup_checkpointer()

    assert "CHECKPOINT_PATH 为空" in str(excinfo.value)
    assert cp.checkpointer_status()["ready"] is False
    assert cp.checkpointer_status()["degraded"] is False


@pytest.mark.asyncio
async def test_sqlite_memory_path_refuses_startup(monkeypatch):
    """CHECKPOINT_PATH=:memory: 只能靠显式 CHECKPOINT_MODE=memory 选择，sqlite 模式拒绝。"""
    monkeypatch.setattr(settings, "checkpoint_path", ":memory:")

    with pytest.raises(cp.CheckpointSetupError) as excinfo:
        await cp.setup_checkpointer()

    assert ":memory:" in str(excinfo.value)
    assert cp.checkpointer_status()["ready"] is False


@pytest.mark.asyncio
async def test_production_memory_mode_is_refused(tmp_path, monkeypatch):
    """生产环境不允许内存 checkpointer：重启后人工审核状态会全部丢失。"""
    monkeypatch.setattr(settings, "checkpoint_mode", "memory")
    monkeypatch.setattr(settings, "app_env", "production")

    with pytest.raises(cp.CheckpointSetupError) as excinfo:
        await cp.setup_checkpointer()

    assert "CHECKPOINT_MODE=memory" in str(excinfo.value)
    assert cp.checkpointer_status()["ready"] is False


@pytest.mark.asyncio
async def test_invalid_checkpoint_mode_is_refused(monkeypatch):
    """拼写错误的 CHECKPOINT_MODE 不能静默当成 sqlite 运行。"""
    monkeypatch.setattr(settings, "checkpoint_mode", "sqltie")

    with pytest.raises(cp.CheckpointSetupError) as excinfo:
        await cp.setup_checkpointer()

    assert "CHECKPOINT_MODE 非法" in str(excinfo.value)
    assert "sqltie" in str(excinfo.value)
    assert cp.checkpointer_status()["ready"] is False


def test_settings_rejects_invalid_checkpoint_mode_from_env(monkeypatch):
    """Settings 构造时（读取环境变量）也拒绝非法模式，不依赖启动后的兜底。"""
    from pydantic import ValidationError

    from app.config import Settings

    monkeypatch.setenv("CHECKPOINT_MODE", "invalid")
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    # Literal 类型在字段校验层就拒绝非法值；自定义 validator 是运行时绕过类型时的兜底。
    assert "sqlite" in str(excinfo.value) and "memory" in str(excinfo.value)


def test_startup_fails_fast_on_invalid_llm_config(monkeypatch):
    """provider 非 none 且配置非法时，应用启动本身必须失败（不只是 /ready 503）。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "llm_provider", "deepseek")
    monkeypatch.setattr(settings, "deepseek_api_key", "")

    with pytest.raises(RuntimeError, match="LLM 配置非法"):
        with TestClient(app):
            pass  # 进入 with 即触发 lifespan；非法配置必须在这一步抛错


@pytest.mark.asyncio
async def test_ready_503_when_llm_config_invalid(monkeypatch):
    monkeypatch.setattr(settings, "checkpoint_mode", "memory")
    await cp.setup_checkpointer()
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/ready")

    assert resp.status_code == 503
    llm = next(c for c in resp.json()["checks"] if c["name"] == "llm_config")
    assert llm["ok"] is False
    assert "Unsupported LLM_PROVIDER=openai" in llm["detail"]


@pytest.mark.asyncio
async def test_startup_failure_closes_runtime_even_before_yield(monkeypatch):
    """初始化中途失败（还没进 yield）也必须关闭共享 HTTP client / checkpointer。

    对应 m02853 第 6 项：旧实现把 finally 只包住 yield，start_llm_runtime() 之后、
    yield 之前任意一步抛错都会把已启动的资源泄漏在半启动的进程里。
    """
    from app.llm import runtime as rt

    # 用真实的 start/stop 驱动进程级 client，才能断言「已创建的 client 真被关闭」。
    calls: list[str] = []
    clients: list = []
    real_start = rt.start_llm_runtime
    real_stop = rt.stop_llm_runtime

    def traced_start():
        calls.append("start_llm_runtime")
        real_start()
        clients.append(rt.shared_client())

    async def traced_stop():
        calls.append("stop_llm_runtime")
        await real_stop()

    async def traced_close():
        calls.append("close_checkpointer")

    def traced_reset():
        calls.append("reset_compiled_graph")

    monkeypatch.setattr(main_mod, "start_llm_runtime", traced_start)
    monkeypatch.setattr(main_mod, "stop_llm_runtime", traced_stop)
    monkeypatch.setattr(main_mod, "close_checkpointer", traced_close)
    monkeypatch.setattr(main_mod, "reset_compiled_graph", traced_reset)

    # 让 memory_store.init() 在 HTTP client 已经启动之后失败，模拟「半启动」。
    async def broken_init():
        calls.append("init_called")
        raise RuntimeError("business db init exploded")

    monkeypatch.setattr(main_mod.memory_store, "init", broken_init)

    # 用 lifespan 作为异步上下文管理器直接驱动，不依赖完整 ASGI 启动。
    with pytest.raises(RuntimeError, match="business db init exploded"):
        async with main_mod.lifespan(app):
            pass  # 不该走到 yield

    # 三个清理钩子全部执行；且启动确实走到了 init 之后才失败。
    assert "start_llm_runtime" in calls
    assert "init_called" in calls
    assert calls.count("stop_llm_runtime") == 1
    assert calls.count("close_checkpointer") == 1
    assert calls.count("reset_compiled_graph") == 1
    # 真实进程级 client 确实被创建、且已被 stop_llm_runtime 关闭并置空，没有泄漏。
    assert clients and clients[0] is not None
    assert clients[0].is_closed is True
    assert rt.shared_client() is None


@pytest.mark.asyncio
async def test_llm_config_failure_closes_runtime(monkeypatch):
    """LLM 配置校验失败（start_llm_runtime 之后）同样走外层 finally 清理。"""
    from app.llm import runtime as rt

    calls: list[str] = []
    clients: list = []
    real_start = rt.start_llm_runtime
    real_stop = rt.stop_llm_runtime

    def traced_start():
        calls.append("start_llm_runtime")
        real_start()
        clients.append(rt.shared_client())

    async def traced_stop():
        calls.append("stop_llm_runtime")
        await real_stop()

    monkeypatch.setattr(main_mod, "start_llm_runtime", traced_start)
    monkeypatch.setattr(main_mod, "stop_llm_runtime", traced_stop)
    monkeypatch.setattr(
        main_mod.llm_client, "validate_llm_config", lambda: "DEEPSEEK_API_KEY is empty"
    )

    with pytest.raises(RuntimeError, match="LLM 配置非法"):
        async with main_mod.lifespan(app):
            pass

    assert calls.count("stop_llm_runtime") == 1
    assert clients and clients[0] is not None
    assert clients[0].is_closed is True
    assert rt.shared_client() is None
