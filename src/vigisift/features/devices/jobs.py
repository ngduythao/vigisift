from vigisift.background import Job
from vigisift.features.devices.handlers import expire_commands
from vigisift.runtime import Runtime


def background_jobs(runtime: Runtime) -> list[Job]:
    async def sweep() -> None:
        await expire_commands(runtime)

    return [Job("command-sweep", runtime.settings.command_sweep_sec, sweep)]
