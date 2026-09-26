import unittest
from unittest import mock

from asc_submit import flows


def localization(loc_id, locale, **attrs):
    return {"id": loc_id, "attributes": {"locale": locale, **attrs}}


class FakeClient(mock.Mock):
    """Client double that answers the handful of GETs the flows issue."""

    def __init__(self, version_localizations=(), app_infos=(), app_info_localizations=None):
        super().__init__()
        self._version_localizations = list(version_localizations)
        self._app_infos = list(app_infos)
        # app_info_id -> localizations
        self._app_info_localizations = app_info_localizations or {}
        self.patched = []
        self.posted = []
        self.get_all = mock.Mock(side_effect=self._get_all)
        self.patch = mock.Mock(side_effect=self._patch)
        self.post = mock.Mock(side_effect=self._post)

    def _get_all(self, path, query=None):
        if path.startswith("/v1/appStoreVersions/") and path.endswith("/appStoreVersionLocalizations"):
            return self._version_localizations
        if path == "/v1/apps/APP/appStoreVersions":
            return []  # create_version resolves first; empty = does not exist yet
        if path == "/v1/apps/APP/appInfos":
            return self._app_infos
        if "/appInfoLocalizations" in path:
            app_info_id = path.split("/")[3]
            return self._app_info_localizations.get(app_info_id, [])
        raise AssertionError(f"unexpected GET {path}")

    def _patch(self, path, body):
        self.patched.append((path, body))

    def _post(self, path, body):
        self.posted.append((path, body))
        # Return a plausible created resource carrying the POSTed attributes.
        return {"data": {"id": "new-id", "attributes": dict(body["data"].get("attributes", {}))}}


class SetLocalizationsKeywordsTests(unittest.TestCase):
    def test_existing_locale_is_patched(self):
        client = FakeClient(version_localizations=[localization("L1", "ja", keywords="old")])
        flows.set_localizations(client, "V", keywords={"ja": "a,b,c"})
        self.assertEqual(len(client.patched), 1)
        path, body = client.patched[0]
        self.assertEqual(path, "/v1/appStoreVersionLocalizations/L1")
        self.assertEqual(body["data"]["attributes"], {"keywords": "a,b,c"})
        self.assertFalse(client.posted)

    def test_missing_locale_is_posted(self):
        client = FakeClient(version_localizations=[])
        flows.set_localizations(client, "V", keywords={"ja": "a,b"})
        self.assertEqual(len(client.posted), 1)
        path, body = client.posted[0]
        self.assertEqual(path, "/v1/appStoreVersions/V/appStoreVersionLocalizations")
        self.assertEqual(body["data"]["attributes"], {"locale": "ja", "keywords": "a,b"})
        self.assertFalse(client.patched)

    def test_over_100_characters_is_rejected_before_any_write(self):
        client = FakeClient(version_localizations=[localization("L1", "ja")])
        with self.assertRaises(SystemExit):
            flows.set_localizations(client, "V", keywords={"ja": "字" * 101})
        self.assertFalse(client.patched and client.posted)

    def test_line_break_is_rejected(self):
        client = FakeClient()
        with self.assertRaises(SystemExit):
            flows.set_localizations(client, "V", keywords={"ja": "a\nb"})
        self.assertFalse(client.patched and client.posted)

    def test_locale_alias_is_normalized(self):
        client = FakeClient(version_localizations=[localization("L1", "ja")])
        flows.set_localizations(client, "V", keywords={"ja-JP": "x"})
        self.assertEqual(client.patched[0][1]["data"]["attributes"], {"keywords": "x"})


class CreateVersionPlatformTests(unittest.TestCase):
    def test_create_version_posts_mac_os_platform(self):
        # The ASC API enum is MAC_OS; OS_X is rejected with HTTP 409 (found live
        # when the CI workflow ran create-version for the first time).
        client = FakeClient()
        flows.create_version(client, "APP", "0.8.1")
        path, body = client.posted[0]
        self.assertEqual(path, "/v1/appStoreVersions")
        self.assertEqual(body["data"]["attributes"]["platform"], "MAC_OS")


class SetSubtitlesTests(unittest.TestCase):
    def _client(self, state="PREPARE_FOR_SUBMISSION"):
        return FakeClient(
            app_infos=[
                {"id": "INFO_LIVE", "attributes": {"state": "READY_FOR_SALE"}},
                {"id": "INFO_EDIT", "attributes": {"state": state}},
            ],
            app_info_localizations={
                "INFO_EDIT": [localization("AL1", "ja", subtitle="旧", name="Kilde")],
            },
        )

    def test_patches_the_editable_app_info_localization(self):
        client = self._client()
        flows.set_subtitles(client, "APP", {"ja": "新しいサブタイトル"})
        self.assertEqual(len(client.patched), 1)
        path, body = client.patched[0]
        self.assertEqual(path, "/v1/appInfoLocalizations/AL1")
        self.assertEqual(body["data"]["attributes"], {"subtitle": "新しいサブタイトル"})

    def test_missing_locale_is_posted_on_the_editable_app_info(self):
        client = self._client()
        flows.set_subtitles(client, "APP", {"en-US": "Record & transcribe"})
        self.assertEqual(len(client.posted), 1)
        path, body = client.posted[0]
        self.assertEqual(path, "/v1/appInfos/INFO_EDIT/appInfoLocalizations")
        self.assertEqual(body["data"]["attributes"], {"locale": "en-US", "subtitle": "Record & transcribe"})

    def test_rejected_states_count_as_editable(self):
        for state in ("REJECTED", "DEVELOPER_REJECTED", "METADATA_REJECTED", "WAITING_FOR_EXPORT_COMPLIANCE"):
            client = FakeClient(
                app_infos=[{"id": "I", "attributes": {"state": state}}],
                app_info_localizations={"I": []},
            )
            flows.set_subtitles(client, "APP", {"ja": "x"})
            self.assertTrue(client.posted, state)

    def test_no_editable_app_info_fails_with_guidance(self):
        client = FakeClient(
            app_infos=[
                {"id": "A", "attributes": {"state": "READY_FOR_SALE"}},
                {"id": "B", "attributes": {"state": "WAITING_FOR_REVIEW"}},
            ],
            app_info_localizations={},
        )
        with self.assertRaises(SystemExit) as ctx:
            flows.set_subtitles(client, "APP", {"ja": "x"})
        self.assertIn("create-version", str(ctx.exception))

    def test_over_30_characters_is_rejected_before_any_write(self):
        client = self._client()
        with self.assertRaises(SystemExit):
            flows.set_subtitles(client, "APP", {"ja": "字" * 31})
        self.assertFalse(client.patched and client.posted)


if __name__ == "__main__":
    unittest.main()
