"""実際の素材を消さず、曲ID・選択・永久タイトル予約の契約を検証する。"""
import array
import math
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_music_catalog as catalog


class MusicReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / "1_review_260929"
        self.folder.mkdir()
        self.env = patch.dict(os.environ, {"APP_MUSIC_CATALOG_DB": str(self.root / "music.sqlite3")})
        self.env.start()
        self.seed = patch.object(catalog, "_seeded", True)
        self.seed.start()
        self.saved = []
        for n, name in enumerate(["Silver Avenue.mp3", "Silver Avenue_2.mp3"]):
            (self.folder / name).write_bytes(f"original-{n}".encode())
            self.saved.append({"song_id": f"clip-{n}", "title": "Silver Avenue", "file": name})
        self.batch = catalog.register_download(self.folder, self.saved, "review", 2)

    def tearDown(self):
        self.seed.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_take_parentheses_are_hidden_in_timestamps_and_description(self):
        from app_track_title import public_track_title
        import app_ffrender, app_premiere, app_timeline, app_core, claude_proposer
        self.assertEqual(public_track_title('Silver Avenue(1)'), 'Silver Avenue')
        self.assertEqual(public_track_title('Silver Avenue （２）'), 'Silver Avenue')
        self.assertEqual(public_track_title('Silver Avenue (Live)'), 'Silver Avenue (Live)')
        self.assertEqual(app_timeline.display_title('01 - Silver Avenue(1).mp3'), 'Silver Avenue')
        self.assertEqual(app_ffrender._title_from('z_Silver Avenue(1).mp3'), 'Silver Avenue')
        clips = [{'start':0,'end':240,'title':'Silver Avenue(1)'},
                 {'start':240,'end':480,'title':'Last Light（２）'}]
        tc = self.folder / 'music_time_code_info_1.txt'
        app_premiere.generate_timecode(clips, tc)
        expected = '00:00:00 - Silver Avenue\n00:04:00 - Last Light'
        self.assertEqual(tc.read_text().strip(), expected)
        app_ffrender.generate_display_timecode(clips, tc, {})
        self.assertEqual(tc.read_text().strip(), expected)
        # 古いファイルから概要欄へ取り込む場合にも番号は表示しない。
        tc.write_text('00:00:00 - Silver Avenue(1)\n00:04:00 - Last Light（２）\n')
        self.assertEqual(app_core._read_matching_timecodes_until_loop(self.folder, '1')[0], expected)
        self.assertEqual(claude_proposer._read_tracklist_for_description(self.folder), expected)
        srt = self.folder / 'review.srt'
        app_premiere.generate_srt(clips, srt)
        self.assertNotIn('(1)', srt.read_text())
        self.assertNotIn('（２）', srt.read_text())

    def test_title_history_is_permanent_and_normalized(self):
        first = catalog.prepare_submission({"title": "ＳＩＬＶＥＲ—ＡＶＥＮＵＥ"})
        self.assertNotEqual(catalog.title_key(first["title"]), catalog.title_key("Silver Avenue"))
        second = catalog.prepare_submission({"title": first["title"]})
        self.assertNotEqual(first["title"], second["title"])
        catalog.mark_submitted(first)
        with catalog.transaction() as conn:
            row = conn.execute("SELECT * FROM titles WHERE request_id=?", (first["_music_request_id"],)).fetchone()
            self.assertEqual(row["state"], "submitted")
            self.assertEqual(json.loads(row["content_json"])["title"], first["title"])

    def test_concurrent_processes_reserve_unique_titles(self):
        script = "import app_music_catalog as c; c._seeded=True; print(c.prepare_submission({'title':'New Current'})['title'])"
        env = {**os.environ, "PYTHONPATH": str(Path(catalog.__file__).parent)}
        procs = [subprocess.Popen([sys.executable, "-c", script], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(5)]
        titles = []
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0, stderr)
            titles.append(stdout.strip().splitlines()[-1])
        self.assertEqual(len({catalog.title_key(t) for t in titles}), 5)

    def test_equal_duration_takes_are_never_auto_selected(self):
        with self.assertRaises(catalog.SelectionPending):
            catalog.process_selected(self.folder, shutil.copyfile)
        self.assertFalse((self.folder / "music").exists())
        self.assertTrue(all((self.folder / t["file"]).exists() for t in self.saved))

    def test_heart_is_exclusive_and_unheart_returns_to_pending(self):
        catalog.choose_take(self.batch, "clip-0", True)
        result = catalog.choose_take(self.batch, "clip-1", True)
        self.assertTrue(result["ready"])
        self.assertEqual([t["clip_id"] for t in result["takes"] if t["selected"]], ["clip-1"])
        result = catalog.choose_take(self.batch, "clip-1", False)
        self.assertFalse(result["ready"])

    def test_only_chosen_take_is_processed_and_originals_unchanged(self):
        catalog.choose_take(self.batch, "clip-1", True)
        catalog.process_selected(self.folder, shutil.copyfile)
        self.assertEqual((self.folder / "music/Silver Avenue.mp3").read_bytes(), b"original-1")
        self.assertEqual((self.folder / "Silver Avenue.mp3").read_bytes(), b"original-0")
        self.assertEqual((self.folder / "Silver Avenue_2.mp3").read_bytes(), b"original-1")
        self.assertTrue(catalog.require_selection(self.folder, processed=True)["processed"])
        catalog.choose_take(self.batch, "clip-0", True)
        with self.assertRaises(catalog.SelectionPending): catalog.require_selection(self.folder, processed=True)
        catalog.process_selected(self.folder, shutil.copyfile)
        self.assertEqual((self.folder / "music/Silver Avenue.mp3").read_bytes(), b"original-0")
        self.assertEqual(next((self.folder / ".music_previous").rglob("*.mp3")).read_bytes(), b"original-1")

    def test_processing_failure_preserves_previous_output(self):
        catalog.choose_take(self.batch, "clip-0", True)
        catalog.process_selected(self.folder, shutil.copyfile)
        catalog.choose_take(self.batch, "clip-1", True)
        def fail(src, dst): raise RuntimeError("ffmpeg failed")
        with self.assertRaises(RuntimeError): catalog.process_selected(self.folder, fail)
        self.assertEqual((self.folder / "music/Silver Avenue.mp3").read_bytes(), b"original-0")
        self.assertFalse(catalog.get_review(self.batch)["processing"])
        self.assertFalse(catalog.get_review(self.batch)["processed"])

    def test_missing_or_incomplete_download_blocks_production(self):
        catalog.choose_take(self.batch, "clip-0", True)
        (self.folder / "Silver Avenue_2.mp3").unlink()
        self.assertFalse(catalog.get_review(self.batch)["ready"])
        with self.assertRaises(catalog.SelectionPending): catalog.require_selection(self.folder)

    def test_partial_registration_waits_until_both_takes_are_available(self):
        folder = self.root / "partial"
        folder.mkdir()
        for item in self.saved: shutil.copyfile(self.folder / item["file"], folder / item["file"])
        batch = catalog.register_download(folder, self.saved[:1], "partial", 2)
        catalog.choose_take(batch, "clip-0", True)
        self.assertFalse(catalog.get_review(batch)["ready"])
        catalog.register_download(folder, self.saved, "partial", 2)
        self.assertTrue(catalog.get_review(batch)["ready"])

    def test_processed_files_removed_externally_block_production(self):
        catalog.choose_take(self.batch, "clip-0", True)
        catalog.process_selected(self.folder, shutil.copyfile)
        (self.folder / "music/Silver Avenue.mp3").unlink()
        with self.assertRaises(catalog.SelectionPending): catalog.require_selection(self.folder, processed=True)

    def test_unmanaged_music_is_not_overwritten(self):
        (self.folder / "music").mkdir()
        (self.folder / "music/existing.mp3").write_bytes(b"legacy")
        catalog.choose_take(self.batch, "clip-0", True)
        with self.assertRaises(ValueError): catalog.process_selected(self.folder, shutil.copyfile)
        self.assertEqual((self.folder / "music/existing.mp3").read_bytes(), b"legacy")

    def test_legacy_processed_folder_is_not_migrated(self):
        folder = self.root / "legacy"
        folder.mkdir()
        (folder / "music").mkdir()
        (folder / "music/Old Song.mp3").write_bytes(b"legacy")
        (folder / ".suno_fast_download.json").write_text(json.dumps({"expected":2,"saved":self.saved}))
        self.assertIsNone(catalog.review_state(folder))

    def test_portable_manifest_restores_selected_clip(self):
        catalog.choose_take(self.batch, "clip-1", True)
        with patch.dict(os.environ, {"APP_MUSIC_CATALOG_DB":str(self.root / "another.sqlite3")}):
            restored = catalog.register_download(self.folder, self.saved, "review", 2)
            self.assertEqual([t["clip_id"] for t in catalog.get_review(restored)["takes"] if t["selected"]], ["clip-1"])

    def test_path_traversal_is_rejected(self):
        outside = self.root / "outside.mp3"
        outside.write_bytes(b"outside")
        with self.assertRaises(ValueError):
            catalog.register_download(self.folder, [{"song_id":"evil","title":"Evil","file":"../outside.mp3"}], "evil", 2)

    def test_pipeline_and_rendering_gate_pending_selection(self):
        import app_pipeline, app_timeline, app_ffrender
        self.assertEqual(app_pipeline.step_rename(1, self.folder, False), "awaiting_selection")
        self.assertEqual(app_pipeline.step_premiere(1, self.folder, False), "awaiting_selection")
        self.assertEqual(app_pipeline.step_export(1, self.folder, False), "awaiting_selection")
        self.assertEqual(app_pipeline.step_upload(1, self.folder, False), "awaiting_selection")
        with self.assertRaises(catalog.SelectionPending): app_timeline._audio_files(self.folder)
        with self.assertRaises(catalog.SelectionPending): app_ffrender.render(self.folder)

    def test_http_hearts_and_video_hearts_share_same_selection(self):
        import app
        from fastapi.testclient import TestClient
        client = TestClient(app.app)
        with patch.object(app, "resolve_video_folder", return_value=self.folder), patch.object(app, "_get_duration", return_value=240):
            self.assertEqual(client.post(f"/api/music/reviews/{self.batch}/process").status_code, 409)
            result = client.post(f"/api/music/reviews/{self.batch}/heart", json={"clip_id":"clip-0","selected":True})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertTrue(result.json()["ready"])
            response = client.post(f"/api/videos/{self.folder.name}/track-like", json={"rel_path":"Silver Avenue_2.mp3","set_likes":1})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual([t["clip_id"] for t in response.json()["review"]["takes"] if t["selected"]], ["clip-1"])
            tracks = client.get(f"/api/videos/{self.folder.name}/tracks").json()["tracks"]
            self.assertEqual(len(tracks), 2)
            self.assertEqual(sum(t["likes"] for t in tracks), 1)
            self.assertEqual(client.delete(f"/api/videos/{self.folder.name}/track", params={"rel_path":"Silver Avenue.mp3"}).status_code, 409)
            self.assertEqual(client.get(f"/api/music/reviews/{self.batch}/audio/clip-1").content, b"original-1")
        self.assertFalse(app._should_auto_resume(79, "rename", 1, {"auto_resume":True})[0])

    def test_shared_submission_reserves_title_without_changing_music(self):
        import suno_auto_create as suno
        content = {"title":"Silver Avenue", "styles":"approved groove", "lyrics":"original lyrics",
                   "mode":"lyrics_styles", "exclude_styles":"ballad", "duration_seconds":240}
        with patch.object(suno, "inject_into_suno", return_value=True) as inject, \
             patch.object(suno, "click_create_button") as create, \
             patch.object(suno, "detect_bot_challenge", return_value=False), \
             patch.object(suno, "detect_copyright_error", return_value=False):
            result = suno._submit_song_to_suno_impl(object(), content)
        self.assertNotEqual(result["title"], content["title"])
        for key in ["styles","lyrics","mode","exclude_styles","duration_seconds"]:
            self.assertEqual(result[key], content[key])
        self.assertEqual(inject.call_args.args[1]["title"], result["title"])
        create.assert_called_once()

    def test_invalid_form_never_clicks_create(self):
        import suno_auto_create as suno
        with patch.object(suno, "inject_into_suno", return_value=False), patch.object(suno, "click_create_button") as create:
            with self.assertRaises(suno.SunoSubmissionError):
                suno._submit_song_to_suno_impl(object(), {"title":"Never Submitted"}, form_retries=0)
        create.assert_not_called()

    def test_generation_api_forwards_duration_excludes_and_auto_download(self):
        import app
        from fastapi.testclient import TestClient
        from unittest.mock import AsyncMock, MagicMock
        fake = MagicMock()
        fake.returncode = 0
        with patch.object(app, "get_suno_config", return_value={}), \
             patch.object(app, "resolve_video_folder", return_value=self.folder), \
             patch.object(app, "get_dashboard_config", return_value={"channel_name":"review"}), \
             patch.object(app, "_ensure_not_running", new=AsyncMock(return_value="test")), \
             patch.object(app, "_acquire_resource_or_409", return_value=MagicMock()), \
             patch.object(app, "_register_active_task"), patch.object(app, "_stream_subprocess"), \
             patch.dict(app.task_meta, {}, clear=True), patch.dict(app.task_logs, {}, clear=True), \
             patch.object(app.subprocess, "Popen", return_value=fake) as popen:
            result = TestClient(app.app).post('/api/suno/start', json={
                "prompt":"Approved beat driven groove generation", "video_name":self.folder.name,
                "auto_download":True, "duration_seconds":240,
                "songs_draft_json":[{"title":"New Title","styles":"approved groove","lyrics":"original lyrics",
                                     "mode":"lyrics_styles","exclude_styles":"ballad","duration_seconds":240}]})
        self.assertEqual(result.status_code, 200, result.text)
        args = popen.call_args.args[0]
        self.assertEqual(args[args.index('--duration-seconds')+1], '240')
        self.assertEqual(args[args.index('--auto-download')+1], str(self.folder))
        self.assertEqual(popen.call_args.kwargs['env']['APP_KEEP_BROWSER'], '0')
        draft_path = Path(args[args.index('--songs-file')+1])
        draft = json.loads(draft_path.read_text())[0]
        draft_path.unlink()
        self.assertEqual(draft['exclude_styles'], 'ballad')
        self.assertEqual(draft['duration_seconds'], 240)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpegが必要です')
    def test_tagging_after_review_keeps_reserved_title_and_selected_take(self):
        import app_process_tracks as processing
        for take in self.saved:
            subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','sine=frequency=440:duration=2',
                            '-c:a','libmp3lame',str(self.folder/take['file'])],check=True,timeout=20)
        catalog.choose_take(self.batch, 'clip-1', True)
        catalog.process_selected(self.folder, shutil.copyfile)
        with patch.object(processing, 'process_mp3', side_effect=shutil.copyfile):
            result = processing.process_folder(self.folder, skip_tagging=False, artist='Review Artist',
                                              album='Review Album', genre='Electronic Soul')
        self.assertEqual(result, 0)
        tags = processing._probe_format_tags(self.folder / 'music/Silver Avenue.mp3')
        self.assertEqual(tags['title'], 'Silver Avenue')
        self.assertEqual(tags['artist'], 'Review Artist')
        self.assertEqual(tags['genre'], 'Electronic Soul')
        self.assertTrue(catalog.require_selection(self.folder, processed=True)['processed'])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpegが必要です')
    def test_equal_filename_and_size_does_not_reuse_previous_take_audio_cache(self):
        import app_ffrender as renderer
        for take, frequency in zip(self.saved, [440, 660]):
            subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i',f'sine=frequency={frequency}:duration=2',
                            '-c:a','libmp3lame','-b:a','128k',str(self.folder/take['file'])],check=True,timeout=20)
        outputs = []
        for selected in [0, 1]:
            catalog.choose_take(self.batch, f'clip-{selected}', True)
            catalog.process_selected(self.folder, shutil.copyfile)
            source = self.folder / 'music/Silver Avenue.mp3'
            outputs.append(source.stat().st_size)
            for mode, build, suffix in [('aac',renderer.build_audio,'.m4a'),('mp3',renderer.build_audio_mp3_copy,'.mp3')]:
                output = self.folder / f'render-{selected}-{mode}{suffix}'
                build([{'path':source}], 1, output, cache_dir=self.folder/'.audio-cache')
                raw = subprocess.run(['ffmpeg','-v','error','-i',str(output),'-t','0.7','-ar','16000','-ac','1','-f','s16le','-'],
                                     capture_output=True,check=True,timeout=20).stdout
                pcm = array.array('h',raw)
                powers = {}
                for frequency in [440,660]:
                    coefficient = 2*math.cos(2*math.pi*frequency/16000)
                    previous = older = 0
                    for value in pcm:
                        current = value + coefficient*previous - older
                        older,previous = previous,current
                    powers[frequency] = previous*previous + older*older - coefficient*previous*older
                self.assertEqual(max(powers,key=powers.get), [440,660][selected])
        self.assertEqual(outputs[0],outputs[1])


if __name__ == "__main__": unittest.main()
