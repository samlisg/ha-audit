"""Engine tests need only pytest + PyYAML. The integration tests need the Home Assistant
test plugin and are skipped automatically when it is not installed."""
import pytest

try:
    import pytest_homeassistant_custom_component  # noqa: F401
except ImportError:  # engine-only run (e.g. the lightweight CI job)
    HAVE_HA_PLUGIN = False
else:
    HAVE_HA_PLUGIN = True
    pytest_plugins = "pytest_homeassistant_custom_component"

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        yield
