"""Environment detection: platform, Termux markers, color support, geometry."""

from __future__ import annotations

from ghostlink.core.environment import EnvironmentDetector
from ghostlink.models.environment import ColorSupport, PlatformKind


class TestPlatformClassification:
    def test_plain_linux(self) -> None:
        kind = EnvironmentDetector.classify_platform({}, "Linux")
        assert kind is PlatformKind.LINUX

    def test_termux_via_version_variable(self) -> None:
        kind = EnvironmentDetector.classify_platform({"TERMUX_VERSION": "0.118.0"}, "Linux")
        assert kind is PlatformKind.TERMUX

    def test_termux_via_prefix(self) -> None:
        kind = EnvironmentDetector.classify_platform(
            {"PREFIX": "/data/data/com.termux/files/usr"}, "Linux"
        )
        assert kind is PlatformKind.TERMUX

    def test_termux_via_home(self) -> None:
        kind = EnvironmentDetector.classify_platform(
            {"HOME": "/data/data/com.termux/files/home"}, "Linux"
        )
        assert kind is PlatformKind.TERMUX

    def test_unsupported_system(self) -> None:
        assert EnvironmentDetector.classify_platform({}, "Darwin") is PlatformKind.UNSUPPORTED
        assert EnvironmentDetector.classify_platform({}, "Windows") is PlatformKind.UNSUPPORTED


class TestColorClassification:
    def test_no_color_variable_wins(self) -> None:
        env = {"NO_COLOR": "1", "COLORTERM": "truecolor"}
        assert EnvironmentDetector.classify_color_support(env) is ColorSupport.NONE

    def test_dumb_terminal(self) -> None:
        assert EnvironmentDetector.classify_color_support({"TERM": "dumb"}) is ColorSupport.NONE

    def test_truecolor(self) -> None:
        assert (
            EnvironmentDetector.classify_color_support({"COLORTERM": "truecolor"})
            is ColorSupport.TRUECOLOR
        )
        assert (
            EnvironmentDetector.classify_color_support({"COLORTERM": "24bit"})
            is ColorSupport.TRUECOLOR
        )

    def test_256_color(self) -> None:
        assert (
            EnvironmentDetector.classify_color_support({"TERM": "xterm-256color"})
            is ColorSupport.EXTENDED
        )

    def test_basic_fallback(self) -> None:
        assert EnvironmentDetector.classify_color_support({}) is ColorSupport.BASIC


class TestDetect:
    def test_detect_returns_snapshot(self) -> None:
        info = EnvironmentDetector.detect()
        assert info.terminal_columns > 0
        assert info.terminal_rows > 0
        assert isinstance(info.platform, PlatformKind)
        assert isinstance(info.supported_python, bool)
        assert info.terminal_size_label == f"{info.terminal_columns}×{info.terminal_rows}"

    def test_termux_snapshot_labels(self) -> None:
        info = EnvironmentDetector.detect(env={"TERMUX_VERSION": "0.118.0"})
        assert info.is_termux
        assert info.platform_label == "Termux (Android)"
