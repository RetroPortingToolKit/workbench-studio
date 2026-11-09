"""Boot serials must agree with the runtime selected for this build."""
from pathlib import Path
import tempfile
import unittest
from pin_builder import pin_build as builder, lean_boot_gate as gate


class BootSerialContractTests(unittest.TestCase):
    def test_serial_and_mounted_report_agree(self):
        cases = [("SLUSP012.06", "SLUS-01206"), ("SLUSP013.46", "SLUS-01346"),
                 ("cdrom:\\slusp012.06;1", "SLUS-01206"), ("SLUS_005.94", "SLUS-00594"),
                 ("SLUS-01206", "SLUS-01206"), ("sles_123.45", "SLES-12345"),
                 ("SLPS_009.98", "SLPS-00998"), ("ABCDP012.06", "ABCDP012.06"),
                 ("SLUSPP012.06", "SLUSPP012.06"), ("PSX.EXE", "PSX.EXE")]
        for name, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(builder.normalize_boot_serial(name), expected)
                self.assertEqual(gate.boot_name_serial(name), expected)
                log = "text image guard armed (0x1000..0x2000, disc " + name + ")"
                self.assertEqual(gate.mounted_serial(log), expected)

    def test_unusual_names_require_capability_but_ordinary_names_keep_old_runtime(self):
        for relative in ("runtime/include/disc_identity.h", "runtime/include/disc_roster.h"):
            with self.subTest(header=relative), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                builder.require_boot_serial_contract(root, ["SLUS_005.94"])
                with self.assertRaisesRegex(builder.BuildStop, "compatible serial normalization"):
                    builder.require_boot_serial_contract(root, ["SLUSP012.06"])
                header = root / relative
                header.parent.mkdir(parents=True)
                header.write_text("#define PSX_DISC_BOOT_SERIAL_VERSION 1\n", encoding="utf-8")
                with self.assertRaises(builder.BuildStop):
                    builder.require_boot_serial_contract(root, ["SLUSP013.46"])
                header.write_text("#define PSX_DISC_BOOT_SERIAL_VERSION 2\n", encoding="utf-8")
                builder.require_boot_serial_contract(root, ["SLUSP012.06", "SLUSP013.46"])
