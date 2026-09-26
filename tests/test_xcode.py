import tempfile
import unittest
from pathlib import Path

from asc_submit import xcode


class ArchiveCommandTests(unittest.TestCase):
    def test_project_command_carries_build_settings(self):
        cmd = xcode.build_archive_command(
            scheme="MyApp",
            configuration="Release",
            platform="macOS",
            version="0.7.0",
            build="9",
            derived_data=Path("/tmp/dd"),
            archive_path=Path("/tmp/MyApp.xcarchive"),
            project="App.xcodeproj",
            team_id="TEAM123",
        )
        self.assertEqual(cmd[0], "xcodebuild")
        self.assertIn("archive", cmd)
        self.assertEqual(cmd[cmd.index("-scheme") + 1], "MyApp")
        self.assertEqual(cmd[cmd.index("-destination") + 1], "generic/platform=macOS")
        self.assertIn("MARKETING_VERSION=0.7.0", cmd)
        self.assertIn("CURRENT_PROJECT_VERSION=9", cmd)
        self.assertIn("DEVELOPMENT_TEAM=TEAM123", cmd)
        self.assertIn("-allowProvisioningUpdates", cmd)

    def test_workspace_variant_omits_project(self):
        cmd = xcode.build_archive_command(
            scheme="S",
            configuration="Release",
            platform="iOS",
            version="1.0",
            build="1",
            derived_data=Path("/tmp/dd"),
            archive_path=Path("/tmp/a.xcarchive"),
            workspace="App.xcworkspace",
        )
        self.assertNotIn("-project", cmd)
        self.assertEqual(cmd[cmd.index("-workspace") + 1], "App.xcworkspace")
        self.assertEqual(cmd[cmd.index("-destination") + 1], "generic/platform=iOS")
        self.assertNotIn("DEVELOPMENT_TEAM=None", cmd)


class ExportOptionsTests(unittest.TestCase):
    def test_options_with_and_without_team(self):
        for team, expect in ((None, False), ("TEAM123", True)):
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "exportOptions.plist"
                xcode.write_export_options(path, team)
                text = path.read_text()
            self.assertIn("<string>app-store-connect</string>", text)
            self.assertIn("<string>upload</string>", text)
            self.assertIn("<string>automatic</string>", text)
            self.assertEqual("<string>TEAM123</string>" in text, expect)


class ArchiveInspectionTests(unittest.TestCase):
    def test_find_app_and_versions(self):
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d) / "MyApp.xcarchive"
            app = archive / "Products" / "Applications" / "MyApp.app"
            (app / "Contents").mkdir(parents=True)
            plist = app / "Contents" / "Info.plist"
            plist.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>'
                "<key>CFBundleShortVersionString</key><string>0.7.0</string>"
                "<key>CFBundleVersion</key><string>9</string>"
                "</dict></plist>"
            )
            found = xcode.find_app_in_archive(archive)
            self.assertEqual(found.name, "MyApp.app")
            self.assertEqual(xcode.read_bundle_versions(found), ("0.7.0", "9"))

    def test_missing_archive_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(xcode.XcodeError):
                xcode.find_app_in_archive(Path(d) / "empty.xcarchive")


if __name__ == "__main__":
    unittest.main()
