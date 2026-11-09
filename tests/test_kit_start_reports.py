"""Kit startup must consume this start's report, never a recent previous one."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from pin_builder import pin_build as builder, workbench_package as package


class KitStartReportTests(unittest.TestCase):
    def run_start(self, root, in_product, fresh):
        kit = root / "kit"
        product = kit / "build-release" / "game.exe"
        product.parent.mkdir(parents=True)
        output = root / "evidence"
        output.mkdir()
        folder = product.parent if in_product else kit
        old = folder / package.RUN_REPORT
        old.write_text(json.dumps({"frame": 999, "exit_origin": "exit_after_ms"}), encoding="utf-8")
        calls = 0

        def processes(_executable):
            nonlocal calls
            calls += 1
            if calls == 1:
                return []
            if calls == 2:
                return [{"pid": 42, "window": 0, "command_line": f'"{product}" --bios "bios.bin" --disc "disc.iso"'}]
            if fresh:
                old.write_text(json.dumps({"frame": 1234, "exit_origin": "exit_after_ms"}), encoding="utf-8")
            return []

        def start(_executable, _cwd, _env, log, *_args, **_kwargs):
            text = package.HANDOVER_LINE + "\n" + str(product) + "\n"
            Path(log).write_text(text, encoding="utf-8")
            return 0, text

        with mock.patch.object(package, "start_setup_program", side_effect=start), \
                mock.patch.object(package, "product_processes", side_effect=processes), \
                mock.patch.object(package, "stop_process") as stop:
            if fresh:
                result = package.kit_exe_start(kit, "game", "disc.iso", "bios.bin", {}, output,
                                               {"commands": []}, stage=2, wait=0.1, poll=0, frames_wait=1)
                self.assertEqual(result["frames"], 1234)
            else:
                with self.assertRaisesRegex(builder.BuildStop, "left no fresh"):
                    package.kit_exe_start(kit, "game", "disc.iso", "bios.bin", {}, output,
                                         {"commands": []}, stage=2, wait=0.1, poll=0, frames_wait=1)
                self.assertFalse(old.exists())
            stop.assert_not_called()

    def test_recent_previous_reports_are_removed_at_both_locations(self):
        for in_product in (False, True):
            with self.subTest(in_product=in_product), tempfile.TemporaryDirectory() as temp:
                self.run_start(Path(temp), in_product, False)

    def test_fresh_report_is_accepted_at_both_locations(self):
        for in_product in (False, True):
            with self.subTest(in_product=in_product), tempfile.TemporaryDirectory() as temp:
                self.run_start(Path(temp), in_product, True)
