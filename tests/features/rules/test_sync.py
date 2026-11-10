from httpx import AsyncClient

from tests.fakes.ports import FakeFrigateApi
from tests.features.rules.test_rules_api import RULES
from vigisift.features.rules.sync import RuleSync
from vigisift.runtime import Runtime


class HeldLock:
    async def acquire(self, key: str, seconds: int) -> bool:
        return False


async def _save_while_frigate_is_off(admin: AsyncClient, frigate: FakeFrigateApi) -> None:
    frigate.failing.add("demo")
    await admin.put("/sites/demo/cameras/shop_aisle/rules", json=RULES)
    frigate.failing.clear()


async def test_a_config_read_error_does_not_block_pushing_pending_rules(
    admin: AsyncClient, frigate: FakeFrigateApi, runtime: Runtime
) -> None:
    await _save_while_frigate_is_off(admin, frigate)
    frigate.config_failing.add("demo")

    pushed = await RuleSync(runtime.session, frigate, runtime.cooldown, 300).run_once(force=True)

    assert pushed == 1
    assert ("demo", "shop_aisle") in frigate.rules


async def test_a_site_another_instance_is_syncing_is_skipped_unless_forced(
    admin: AsyncClient, frigate: FakeFrigateApi, runtime: Runtime
) -> None:
    await _save_while_frigate_is_off(admin, frigate)
    sync = RuleSync(runtime.session, frigate, HeldLock(), 300)

    assert await sync.run_once() == 0
    assert await sync.run_once(force=True) == 1


async def test_a_failing_site_is_logged_and_the_pass_still_returns(
    admin: AsyncClient, frigate: FakeFrigateApi, runtime: Runtime
) -> None:
    await _save_while_frigate_is_off(admin, frigate)
    frigate.failing.add("demo")

    assert await RuleSync(runtime.session, frigate, runtime.cooldown, 300).run_once(force=True) == 0
