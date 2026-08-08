"""[transfer] settings validation and limits plumbing (Phase 4)."""

from __future__ import annotations

import pytest

from ghostlink.exceptions.config import ConfigValidationError
from ghostlink.models.settings import AppSettings, TransferSettings
from ghostlink.transfer.manager import TransferLimits


class TestTransferSettings:
    def test_defaults_are_valid(self) -> None:
        settings = TransferSettings()
        assert settings.max_file_size_mb == 100
        assert settings.chunk_size_kb == 4
        assert settings.download_dir == ""

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("max_file_size_mb", 0),
            ("max_file_size_mb", 4097),
            ("max_file_size_mb", "100"),
            ("max_concurrent_transfers", 0),
            ("max_concurrent_transfers", 11),
            ("chunk_size_kb", 0),
            ("chunk_size_kb", 8),
            ("ack_timeout_seconds", 0.1),
            ("ack_timeout_seconds", 61.0),
            ("retry_limit", 0),
            ("retry_limit", 21),
            ("transfer_expiry_minutes", 0),
            ("transfer_expiry_minutes", 1441),
            ("temp_storage_limit_mb", 15),
            ("temp_storage_limit_mb", 65537),
            ("download_dir", 42),
        ],
    )
    def test_invalid_values_rejected(self, field: str, value: object) -> None:
        with pytest.raises(ConfigValidationError, match="transfer\\."):
            TransferSettings(**{field: value})  # type: ignore[arg-type]

    def test_chunk_capacity_cross_check(self) -> None:
        # 4096 MB at 1 KiB chunks would need ~4.2M chunks — far over 32768.
        with pytest.raises(ConfigValidationError, match="chunks"):
            TransferSettings(max_file_size_mb=4096, chunk_size_kb=1)
        # The same ceiling at 4 KiB chunks fits the bitmap.
        TransferSettings(max_file_size_mb=128, chunk_size_kb=4)

    def test_app_settings_carry_transfer_section(self) -> None:
        settings = AppSettings.defaults()
        assert isinstance(settings.transfer, TransferSettings)

    def test_limits_projection_uses_bytes(self) -> None:
        limits = TransferLimits.from_settings(
            TransferSettings(
                max_file_size_mb=2,
                max_concurrent_transfers=4,
                chunk_size_kb=1,
                ack_timeout_seconds=2.5,
                retry_limit=3,
                transfer_expiry_minutes=30,
                temp_storage_limit_mb=32,
            )
        )
        assert limits.max_file_size_bytes == 2 * 1024 * 1024
        assert limits.max_concurrent == 4
        assert limits.chunk_size_bytes == 1024
        assert limits.ack_timeout_seconds == 2.5
        assert limits.retry_limit == 3
        assert limits.temp_limit_bytes == 32 * 1024 * 1024
        assert limits.transfer_expiry_seconds == 1800.0


class TestConfigPlumbing:
    def test_transfer_section_loads_from_toml(self, tmp_path, monkeypatch) -> None:
        from ghostlink.config.loader import validate_sections

        sections = validate_sections(
            {"transfer": {"max_file_size_mb": 12, "chunk_size_kb": 2}}, source="inline"
        )
        assert sections["transfer"]["max_file_size_mb"] == 12

    def test_unknown_transfer_key_rejected(self) -> None:
        from ghostlink.config.loader import validate_sections

        with pytest.raises(ConfigValidationError):
            validate_sections({"transfer": {"backdoor": True}}, source="inline")

    def test_default_config_template_has_transfer_section(self) -> None:
        from ghostlink.assets.branding import load_default_config_text

        template = load_default_config_text()
        assert "[transfer]" in template
        assert "download_dir" in template
        assert "temp_storage_limit_mb" in template
