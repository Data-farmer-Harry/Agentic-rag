from app.api.app import create_app
from app.bootstrap import build_components

components = build_components()
app = create_app(
    components.run_service,
    components.workspace_service,
    hermes_bridge=components.hermes_bridge,
    personal=components.personal_service,
    settings=components.settings,
    startup_callback=components.start,
    shutdown_callback=components.close,
)
