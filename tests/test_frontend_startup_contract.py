from pathlib import Path


def test_splash_screen_defines_timeout_state_used_by_template():
    source = Path("myapp/src/components/SplashScreen.vue").read_text(encoding="utf-8")

    assert 'v-if="isTimeout"' in source
    assert "const isTimeout = computed(" in source
