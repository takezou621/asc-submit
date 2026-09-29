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
        # 明示が必要: 自動生成の子モックは FakeClient の __init__ を parent= 付きで
        # 呼ぼうとして TypeError になる (Python 3.14 実測)
        self.delete = mock.Mock()

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


class AttachBuildTests(unittest.TestCase):
    def _client(self):
        client = FakeClient()
        # find_build lists app-wide builds through get_all("/v1/apps/APP/builds")
        client.get_all = mock.Mock(
            side_effect=lambda path, query=None: (
                [{"id": "B8", "attributes": {"version": "8", "processingState": "VALID"}}]
                if path == "/v1/apps/APP/builds"
                else []
            )
        )
        return client

    def test_patches_the_singular_build_relationship(self):
        # appStoreVersion.build is to-one; "builds" answers 404 (found live).
        client = self._client()
        flows.attach_build(client, "V", "APP", "8")
        path, body = client.patched[0]
        self.assertEqual(path, "/v1/appStoreVersions/V/relationships/build")
        self.assertEqual(body["data"], {"type": "builds", "id": "B8"})


class SubmitForReviewTests(unittest.TestCase):
    """The reviewSubmissions flow (the legacy appStoreVersionSubmissions POST
    is deprecated: 403 "Allowed operation is: DELETE" — kilde run 36284583404)."""

    def _client(self, existing=None):
        client = FakeClient()
        # GET reviewSubmissions for the version → existing (or 404-equivalent [])
        client.get = mock.Mock(return_value={"data": existing or []})
        return client

    def test_creates_links_and_patches_submitted_true(self):
        client = self._client()
        posts = []

        def fake_post(path, body):
            posts.append((path, body))
            if path == "/v1/reviewSubmissions":
                return {"data": {"id": "SUB1", "attributes": {"state": "DRAFT"}}}
            return {"data": {"id": "ITEM1"}}

        client.post = mock.Mock(side_effect=fake_post)
        flows.submit_for_review(client, "APP", "V")
        # 1) create with the app relationship, WITHOUT the version
        self.assertEqual(posts[0][0], "/v1/reviewSubmissions")
        self.assertEqual(posts[0][1]["data"]["attributes"], {"platform": "MAC_OS"})
        self.assertEqual(posts[0][1]["data"]["relationships"]["app"]["data"]["id"], "APP")
        self.assertNotIn("appStoreVersionForReview", posts[0][1]["data"]["relationships"])
        # 2) link the version via reviewSubmissionItems
        self.assertEqual(posts[1][0], "/v1/reviewSubmissionItems")
        rel = posts[1][1]["data"]["relationships"]
        self.assertEqual(rel["reviewSubmission"]["data"]["id"], "SUB1")
        self.assertEqual(rel["appStoreVersion"]["data"]["id"], "V")
        # 3) submit
        path, body = client.patch.call_args[0]
        self.assertEqual(path, "/v1/reviewSubmissions/SUB1")
        self.assertEqual(body["data"]["attributes"], {"submitted": True})

    def test_existing_submission_is_reused(self):
        client = self._client(existing=[{"id": "SUB9", "attributes": {"state": "READY_FOR_REVIEW"}}])
        client.post = mock.Mock(return_value={"data": {"id": "ITEM1"}})
        flows.submit_for_review(client, "APP", "V")
        create_calls = [c for c in client.post.call_args_list if c[0][0] == "/v1/reviewSubmissions"]
        self.assertEqual(create_calls, [])
        path, _ = client.patch.call_args[0]
        self.assertEqual(path, "/v1/reviewSubmissions/SUB9")


class CancelSubmissionTests(unittest.TestCase):
    def test_cancels_via_review_submissions_when_present(self):
        client = FakeClient()
        client.get = mock.Mock(
            return_value={"data": [{"id": "SUB9", "attributes": {"state": "WAITING_FOR_REVIEW"}}]}
        )
        flows.cancel_submission(client, "V")
        (path,) = client.delete.call_args[0]
        self.assertEqual(path, "/v1/reviewSubmissions/SUB9")

    def test_legacy_fallback_when_no_review_submission(self):
        client = FakeClient()
        client.get = mock.Mock(
            side_effect=lambda path, query=None: (
                {"data": {"id": "LEGACY"}} if "appStoreVersionSubmission" in path else {"data": []}
            )
        )
        flows.cancel_submission(client, "V")
        self.assertTrue(client.delete.called)
        (path,) = client.delete.call_args[0]
        self.assertEqual(path, "/v1/appStoreVersionSubmissions/LEGACY")


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


class SetLocalizationsNewFieldsTests(unittest.TestCase):
    def test_promotional_text_patches_existing_locale(self):
        client = FakeClient(version_localizations=[localization("L1", "ja")])
        flows.set_localizations(client, "V", promotional_text={"ja": "新機能！"})
        path, body = client.patched[0]
        self.assertEqual(path, "/v1/appStoreVersionLocalizations/L1")
        self.assertEqual(body["data"]["attributes"], {"promotionalText": "新機能！"})

    def test_urls_are_validated_before_any_write(self):
        client = FakeClient(version_localizations=[localization("L1", "ja")])
        with self.assertRaises(SystemExit):
            flows.set_localizations(client, "V", support_urls={"ja": "ftp://example.com"})
        with self.assertRaises(SystemExit):
            flows.set_localizations(client, "V", marketing_urls={"ja": "example.com"})
        self.assertFalse(client.patched and client.posted)

    def test_promotional_text_limit_is_enforced(self):
        client = FakeClient(version_localizations=[localization("L1", "ja")])
        with self.assertRaises(SystemExit):
            flows.set_localizations(client, "V", promotional_text={"ja": "字" * 171})
        self.assertFalse(client.patched and client.posted)

    def test_mixed_fields_merge_into_one_request_per_locale(self):
        client = FakeClient(version_localizations=[])
        flows.set_localizations(client, "V", whats_new={"ja": "x"}, support_urls={"ja": "https://kilde.app/"})
        self.assertEqual(len(client.posted), 1)
        attrs = client.posted[0][1]["data"]["attributes"]
        self.assertEqual(attrs, {"locale": "ja", "whatsNew": "x", "supportUrl": "https://kilde.app/"})


class SetAppInfoFieldsTests(unittest.TestCase):
    def _client(self):
        return FakeClient(
            app_infos=[{"id": "INFO_EDIT", "attributes": {"state": "PREPARE_FOR_SUBMISSION"}}],
            app_info_localizations={
                "INFO_EDIT": [localization("AL1", "ja", name="Kilde", subtitle="旧")]
            },
        )

    def test_names_and_privacy_urls_patch_the_editable_localization(self):
        client = self._client()
        flows.set_app_info_fields(
            client, "APP", names={"ja": "Kilde 2"}, privacy_urls={"ja": "https://kilde.app/privacy"}
        )
        path, body = client.patched[0]
        self.assertEqual(path, "/v1/appInfoLocalizations/AL1")
        self.assertEqual(
            body["data"]["attributes"],
            {"name": "Kilde 2", "privacyPolicyUrl": "https://kilde.app/privacy"},
        )

    def test_name_limit_is_enforced_before_any_write(self):
        client = self._client()
        with self.assertRaises(SystemExit):
            flows.set_app_info_fields(client, "APP", names={"ja": "字" * 31})
        self.assertFalse(client.patched and client.posted)

    def test_bad_privacy_url_is_rejected(self):
        client = self._client()
        with self.assertRaises(SystemExit):
            flows.set_app_info_fields(client, "APP", privacy_urls={"ja": "kilde.app/privacy"})

    def test_set_subtitles_delegates_to_set_app_info_fields(self):
        client = self._client()
        flows.set_subtitles(client, "APP", {"ja": "新"})
        self.assertEqual(client.patched[0][1]["data"]["attributes"], {"subtitle": "新"})


class ReleaseControlsTests(unittest.TestCase):
    def _client(self, phased=None):
        client = FakeClient()
        client.get = mock.Mock(
            side_effect=lambda path, query=None: (
                {"data": phased} if path.endswith("appStoreVersionPhasedRelease") else {"data": []}
            )
        )
        return client

    def test_set_copyright_patches_the_version(self):
        client = FakeClient()
        flows.set_copyright(client, "V", "© 2026 Kilde")
        path, body = client.patched[0]
        self.assertEqual(path, "/v1/appStoreVersions/V")
        self.assertEqual(body["data"]["attributes"], {"copyright": "© 2026 Kilde"})

    def test_enable_phased_release_posts_with_relationship(self):
        client = self._client(phased=None)
        flows.enable_phased_release(client, "V")
        path, body = client.posted[0]
        self.assertEqual(path, "/v1/appStoreVersionPhasedReleases")
        self.assertEqual(
            body["data"]["relationships"]["appStoreVersion"]["data"],
            {"type": "appStoreVersions", "id": "V"},
        )

    def test_enable_is_idempotent_when_already_enabled(self):
        client = self._client(phased={"id": "PR1", "attributes": {"state": "INACTIVE"}})
        flows.enable_phased_release(client, "V")
        self.assertFalse(client.posted)

    def test_disable_deletes_the_resource(self):
        client = self._client(phased={"id": "PR1", "attributes": {"state": "ACTIVE"}})
        flows.disable_phased_release(client, "V")
        (path,) = client.delete.call_args[0]
        self.assertEqual(path, "/v1/appStoreVersionPhasedReleases/PR1")

    def test_disable_without_resource_is_a_noop(self):
        client = self._client(phased=None)
        flows.disable_phased_release(client, "V")
        self.assertFalse(client.delete.called)

    def test_set_release_date_patches_scheduled_type_and_datetime(self):
        client = FakeClient()
        when = "2027-01-15T09:00:00+09:00"
        flows.set_release_date(client, "V", when)
        path, body = client.patched[0]
        self.assertEqual(path, "/v1/appStoreVersions/V")
        self.assertEqual(body["data"]["attributes"]["releaseType"], "SCHEDULED")
        self.assertEqual(body["data"]["attributes"]["releaseDateTime"], when)

    def test_release_date_requires_a_utc_offset(self):
        with self.assertRaises(SystemExit) as ctx:
            flows.set_release_date(FakeClient(), "V", "2027-01-15T09:00:00")
        self.assertIn("UTC offset", str(ctx.exception))

    def test_release_date_rejects_the_past(self):
        with self.assertRaises(SystemExit) as ctx:
            flows.set_release_date(FakeClient(), "V", "2020-01-01T09:00:00+09:00")
        self.assertIn("past", str(ctx.exception))

    def test_release_date_rejects_garbage(self):
        with self.assertRaises(SystemExit):
            flows.set_release_date(FakeClient(), "V", "next tuesday")

    def test_release_date_accepts_z_suffix(self):
        client = FakeClient()
        flows.set_release_date(client, "V", "2027-01-15T00:00:00Z")
        self.assertEqual(
            client.patched[0][1]["data"]["attributes"]["releaseDateTime"],
            "2027-01-15T00:00:00+00:00",
        )


class WaitForReviewTests(unittest.TestCase):
    def _client(self, states):
        """GET appStoreVersions answers one version whose state advances per poll."""
        holder = {"i": 0}

        def fake_get_all(path, query=None):
            self.assertEqual(path, "/v1/apps/APP/appStoreVersions")
            i = min(holder["i"], len(states) - 1)
            holder["i"] += 1
            return [
                {
                    "id": "V1",
                    "attributes": {"versionString": "0.9.0", "appStoreState": states[i]},
                }
            ]

        client = mock.Mock()
        client.get_all = mock.Mock(side_effect=fake_get_all)
        return client

    def test_resolves_through_transitions(self):
        logs = []
        version = flows.wait_for_review(
            self._client(["WAITING_FOR_REVIEW", "IN_REVIEW", "READY_FOR_SALE"]),
            "APP",
            "0.9.0",
            poll=0,
            log=logs.append,
        )
        self.assertEqual(version["attributes"]["appStoreState"], "READY_FOR_SALE")
        self.assertEqual(logs, ["state: WAITING_FOR_REVIEW", "state: IN_REVIEW", "state: READY_FOR_SALE"])

    def test_already_released_returns_immediately(self):
        version = flows.wait_for_review(self._client(["READY_FOR_SALE"]), "APP", "0.9.0", poll=0)
        self.assertEqual(version["attributes"]["appStoreState"], "READY_FOR_SALE")

    def test_rejection_is_returned_not_raised(self):
        version = flows.wait_for_review(
            self._client(["WAITING_FOR_REVIEW", "REJECTED"]), "APP", "0.9.0", poll=0
        )
        self.assertEqual(version["attributes"]["appStoreState"], "REJECTED")

    def test_unsubmitted_draft_exits_with_guidance(self):
        with self.assertRaises(SystemExit) as ctx:
            flows.wait_for_review(self._client(["PREPARE_FOR_SUBMISSION"]), "APP", "0.9.0", poll=0)
        self.assertIn("asc-submit submit", str(ctx.exception))

    def test_timeout_raises(self):
        with self.assertRaises(SystemExit) as ctx:
            flows.wait_for_review(
                self._client(["IN_REVIEW"]), "APP", "0.9.0", timeout=0.05, poll=0.01
            )
        self.assertIn("Timed out", str(ctx.exception))

    def test_export_compliance_state_carries_a_hint(self):
        logs = []
        flows.wait_for_review(
            self._client(["WAITING_FOR_EXPORT_COMPLIANCE", "IN_REVIEW", "READY_FOR_SALE"]),
            "APP",
            "0.9.0",
            poll=0,
            log=logs.append,
        )
        self.assertTrue(any("export-compliance" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
