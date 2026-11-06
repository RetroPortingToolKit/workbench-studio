import argparse
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import artwork
import backend
import native_bridge
import pinned_build
from pin_builder import pin_build as pb
import player_metadata as metadata
import selection
import settings
from test_backend import synthetic_disc


def authored_disc(root, serial):
    root.mkdir()
    cue = synthetic_disc(root)
    track = root / 'track.bin'
    old = b'SLUS_000.01'
    boot = (serial[:4] + '_' + serial[5:8] + '.' + serial[8:]).encode()
    track.write_bytes(track.read_bytes().replace(old, boot))
    return cue


def page(serial, players, multitap='No'):
    return f'<table><tr><td>Serial Number(s)</td><td>{serial}</td></tr><tr><td>Number Of Players</td><td>&nbsp;{players}</td></tr><tr><td>Multi-Tap Function Compatible</td><td>{multitap}</td></tr></table>'


class PlayerChecks(unittest.TestCase):
    def test_zero_padded_page_and_multitap_maximum(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(metadata, 'CATALOG', Path(tmp) / 'empty.json'):
            root = Path(tmp)
            metadata.CATALOG.write_text('{}')
            text = page('SCUS-94263', '1 or 2 Players').replace('</tr>', '</tr>\n')
            raw = b'\xff\xfe' + text.encode('utf-16-le').replace(b'\n\x00', b'\x00\n')
            with mock.patch.object(metadata.urllib.request, 'urlopen', return_value=io.BytesIO(raw)) as fetch:
                self.assertEqual(metadata.resolve('SCUS-94263', root)['players'], 2)
                fetch.assert_called_once()
            self.assertEqual(metadata.parse_page('SCES-01312', page('SCES-01312', '1 or 2 Players', 'Yes ( 1 to 5 Players )')), 5)
            self.assertEqual(metadata.parse_page('SLES-00785', page('SLES-00785', '1-4 Players', 'Yes ( 1 to 4 Players )')), 4)
            # A pre-fix two-pad cache must not hide the multitap maximum.
            cache = root / 'player-metadata/SCES-01312.json'
            cache.write_text(json.dumps(dict(serial='SCES-01312', players=2, source='https://psxdatacenter.com/games/SCES-01312.html')))
            with mock.patch.object(metadata.urllib.request, 'urlopen', return_value=io.BytesIO(page('SCES-01312', '1 or 2 Players', 'Yes ( 1 to 5 Players )').encode())):
                self.assertEqual(metadata.resolve('SCES-01312', root)['players'], 5)

    def test_unknown_metadata_allows_explicit_override_without_caching_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cue = authored_disc(root / 'unknown', 'SCUS-94263')
            with mock.patch.object(metadata, 'resolve', side_effect=ValueError('metadata unavailable')) as resolve:
                jobs = selection.group_files([cue])
                metadata.identify(jobs, root, allow_unknown=True)
                self.assertIsNone(jobs[0].players)
                self.assertIn('unavailable', jobs[0].player_error)
                resolve.reset_mock()
                jobs = selection.group_files([cue], player_overrides=[4])
                metadata.identify(jobs, root)
                resolve.assert_not_called()
                self.assertEqual(jobs[0].players, 4)
                self.assertEqual(jobs[0].player_metadata['source'], 'manual override')
                self.assertFalse((root / 'player-metadata').exists())
            for invalid in ([0], [9], [True], ['2'], []):
                with self.assertRaises(ValueError):
                    selection.group_files([cue], player_overrides=invalid)

    def test_manual_override_updates_matching_kit_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            kit = Path(tmp)
            (kit / 'project-manifest.toml').write_text('[project]\nplayers=2\n')
            recipe = kit / 'CMakeLists.txt'
            recipe.write_text('psxrecomp_add_game_runtime(game\n    MAX_PLAYERS 2\n)\n')
            args = argparse.Namespace(players=5, override_players=False)
            self.assertEqual(pb.operator_players(args, kit), 2)
            args.override_players = True
            self.assertEqual(pb.operator_players(args, kit), 5)
            pb.override_kit_player_capacity(kit, 5)
            self.assertIn('MAX_PLAYERS 5', recipe.read_text())
            args.players = None
            with self.assertRaises(pb.BuildStop):
                pb.operator_players(args, kit)

    def test_metadata_uses_serial_and_reports_single_and_two_player_games(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trap = authored_disc(root / 'trap', 'SLUS-00679')
            crash = authored_disc(root / 'crash', 'SCUS-94900')
            jobs = selection.group_files([trap, crash], True)
            with mock.patch.object(metadata.urllib.request, 'urlopen', side_effect=AssertionError('unexpected network')):
                metadata.identify(jobs, root / 'data')
                self.assertEqual([g.players for g in jobs], [2, 1])
                self.assertEqual([g.player_metadata['serial'] for g in jobs], ['SLUS-00679', 'SCUS-94900'])
            # ISO uses the same real boot-serial reader, not a filename guess.
            iso = root / 'renamed.iso'
            iso.write_bytes((trap.parent / 'track.bin').read_bytes())
            self.assertEqual(artwork.detect(iso, root), 'SLUS-00679')

    def test_matched_kit_declaration_has_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kit = root / 'cache/pins/recipes/fixture'
            kit.mkdir(parents=True)
            (kit / 'game.toml').write_text('[game]\nid="SLUS-00679"\n')
            (kit / 'CMakeLists.txt').touch()
            (kit / 'project-manifest.toml').write_text('[project]\nplayers=4\n')
            with mock.patch.dict(os.environ, {'WORKBENCH_PUBLIC_CACHE_ROOT': str(root / 'cache')}):
                record = metadata.resolve('SLUS-00679', root)
            self.assertEqual(record['players'], 4)
            self.assertIn('project-manifest.toml', record['source'])

    def test_uncatalogued_serial_lookup_cache_and_invalid_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(metadata.urllib.request, 'urlopen', return_value=io.BytesIO(page('SLUS-00001', '1 to 4 Players').encode())) as fetch:
                record = metadata.resolve('SLUS-00001', root)
                self.assertEqual(record['players'], 4)
                fetch.assert_called_once_with('https://psxdatacenter.com/games/SLUS-00001.html', timeout=10)
            with mock.patch.object(metadata.urllib.request, 'urlopen', side_effect=AssertionError('cache bypassed')):
                self.assertEqual(metadata.resolve('SLUS-00001', root), record)
            (root / 'player-metadata/SLUS-00001.json').write_text('[]')
            with mock.patch.object(metadata.urllib.request, 'urlopen', return_value=io.BytesIO(page('SLUS-00001', '1 to 4 Players').encode())):
                self.assertEqual(metadata.resolve('SLUS-00001', root)['players'], 4)
            for count in ('1 or 2 Players', '1-2 Players', '1 Player'):
                self.assertIn(metadata.parse_page('SLUS-00001', page('SLUS-00001', count)), (1, 2))
            for text in (page('SLUS-99999', '2 Players'), page('SLUS-00001', 'Unknown'), page('SLUS-00001', '9 Players')):
                with self.assertRaises(ValueError):
                    metadata.parse_page('SLUS-00001', text)
            with mock.patch.object(metadata.urllib.request, 'urlopen', side_effect=OSError('offline')):
                with self.assertRaisesRegex(ValueError, 'no player count was guessed'):
                    metadata.resolve('SLUS-00002', root)

    def test_native_selection_ignores_manual_counts_and_returns_identified_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cue = authored_disc(root / 'trap', 'SLUS-00679')
            request = root / 'request.json'
            request.write_text(json.dumps(dict(action='selection', data_root=str(root / 'data'), files=[str(cue)], players=[1])))
            with mock.patch('sys.argv', ['native_bridge.py', str(request)]), mock.patch.object(native_bridge, 'emit') as emit:
                native_bridge.main()
            game = emit.call_args.args[0]['games'][0]
            self.assertEqual(game['players'], 2)
            self.assertEqual(game['player_metadata']['serial'], 'SLUS-00679')

    def test_native_unknown_game_remains_selectable_and_build_accepts_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cue = authored_disc(root / 'unknown', 'SCUS-94263')
            request = root / 'request.json'
            value = dict(action='selection', data_root=str(root / 'data'), files=[str(cue)])
            request.write_text(json.dumps(value))
            with mock.patch('sys.argv', ['native_bridge.py', str(request)]), \
                 mock.patch.object(metadata, 'resolve', side_effect=ValueError('metadata unavailable')) as resolve, \
                 mock.patch.object(native_bridge, 'emit') as emit, mock.patch.dict(os.environ, {}, clear=False):
                native_bridge.main()
                game = emit.call_args.args[0]['games'][0]
                self.assertIsNone(game['players'])
                self.assertIsNone(game['player_override'])
                self.assertIn('unavailable', game['player_error'])
                value.update(action='build', player_overrides=[5])
                request.write_text(json.dumps(value))
                resolve.reset_mock()
                def build(jobs, prefs, data, runner):
                    metadata.identify(jobs, data)
                    self.assertEqual(jobs[0].players, 5)
                    self.assertEqual(jobs[0].player_metadata['source'], 'manual override')
                    return {'state': 'completed'}
                with mock.patch.object(native_bridge, 'build_batch', side_effect=build):
                    native_bridge.main()
                resolve.assert_not_called()

    def test_supported_batch_passes_identified_count_to_real_parser_and_scaffold(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            trap = authored_disc(root / 'trap', 'SLUS-00679')
            crash = authored_disc(root / 'crash', 'SCUS-94900')
            core = root / 'core'
            (core / 'tools').mkdir(parents=True)
            archives = {key: 'fixture/archive.zip' for key in ('python_url', 'python_sha256', 'tcc_url', 'tcc_sha256')}
            (core / 'tools/release_stage.py').write_text('TOOLCHAIN_PINS = ' + repr({'win': archives}))
            sdk = root / 'sdk'
            (sdk / 'python').mkdir(parents=True)
            (sdk / 'python/python.exe').touch()
            pin = root / 'PIN.json'
            pin.write_text('{}')
            bios = root / 'bios.bin'
            bios.write_bytes(b'fixture BIOS')
            prefs = settings.Settings(core_pin=str(pin), output_folder=str(root / 'out'), skip_bios=True, bios_files=[str(bios)])
            jobs = selection.group_files([trap, crash], True)
            jobs.append(selection.GameJob('Explicit override fixture', [trap], player_override=5))
            seen = []
            runner = backend.Runner(lambda line: None)
            def run(argv, cwd, **kwargs):
                if Path(argv[1]).name == 'toolchain_request.py':
                    Path(argv[-1]).write_text(json.dumps({'bin': str(sdk / 'bin')}))
                elif any(Path(str(arg)).name == 'pin_entry.py' for arg in argv):
                    command = [str(arg) for arg in argv[argv.index('build'):]]
                    parser = argparse.ArgumentParser()
                    pb.add_commands(parser.add_subparsers(dest='command'))
                    args, _ = parser.parse_known_args(command)
                    self.assertIn('--players', command)
                    count = pb.operator_players(args)
                    self.assertEqual(args.override_players, count == 5)
                    args.output.mkdir(parents=True)
                    config = args.output / 'game.toml'
                    config.write_text('[game]\nname="Fixture"\nexe="disc/fixture.exe"\nplayers=1\n')
                    pb.set_game_players(config, count)
                    pb.scaffold_kit(args.output, {}, config, 'SCPH1001', count, {'inputs': []})
                    self.assertIn(f'players={count}', config.read_text())
                    self.assertIn(f'MAX_PLAYERS {count}', (args.output / 'CMakeLists.txt').read_text())
                    seen.append(count)
                return ''
            stack.enter_context(mock.patch.object(pinned_build, 'checkout', return_value=core))
            stack.enter_context(mock.patch.object(pb, 'load_pin', return_value={'commit': 'a'*40, 'repo': 'fixture', 'recomp_ui_ref': 'b'*40}))
            stack.enter_context(mock.patch.object(pinned_build.buildtools, 'environment', return_value=os.environ.copy()))
            stack.enter_context(mock.patch.object(pinned_build, 'download'))
            stack.enter_context(mock.patch.object(pb, 'bios_profiles', return_value={}))
            stack.enter_context(mock.patch('delivery.deliver', return_value=(root / 'out', {})))
            stack.enter_context(mock.patch('delivery.remove_owned_tree'))
            stack.enter_context(mock.patch.object(runner, 'run', side_effect=run))
            stack.enter_context(mock.patch.dict(os.environ, {'WORKBENCH_PUBLIC_CACHE_ROOT': str(root/'cache'), 'WORKBENCH_PUBLIC_WORK_ROOT': str(root/'work')}))
            result = pinned_build.batch(jobs, prefs, root/'data', runner)
            self.assertEqual(result['state'], 'completed', result['games'])
            self.assertEqual(seen, [2, 1, 5])
            self.assertEqual([g['player_metadata']['serial'] for g in result['games']], ['SLUS-00679', 'SCUS-94900', 'SLUS-00679'])
            self.assertEqual(result['games'][2]['player_metadata']['source'], 'manual override')

    def test_advanced_batch_uses_identified_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trap = authored_disc(root / 'trap', 'SLUS-00679')
            crash = authored_disc(root / 'crash', 'SCUS-94900')
            jobs = selection.group_files([trap, crash], True)
            prefs = settings.Settings(output_folder=str(root / 'out'), skip_bios=True)
            seen = []
            def build(options, *args, **kwargs):
                seen.append(options.players)
                return {'output': options.output}
            with mock.patch.object(backend.shutil, 'which', return_value='pwsh'), \
                 mock.patch.object(backend, 'snapshot_source', return_value={}), \
                 mock.patch.object(backend.buildtools, 'prepare', return_value={'receipt': {}}), \
                 mock.patch.object(backend, 'build', side_effect=build), \
                 mock.patch('delivery.deliver', return_value=(root / 'out', {})):
                backend.build_batch(jobs, prefs, root / 'data', backend.Runner(lambda line: None))
            self.assertEqual(seen, [2, 1])

    def test_advanced_portable_refresh_keeps_identified_single_player(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cue = authored_disc(root / 'crash', 'SCUS-94900')
            options = backend.BuildOptions([str(cue)], '', str(root / 'out'), skip_bios=True)
            commands = []
            runner = backend.Runner(lambda line: None)
            def run(argv, cwd, **kwargs):
                argv = [str(arg) for arg in argv]
                commands.append(argv)
                if '--json-out' in argv:
                    Path(argv[argv.index('--json-out') + 1]).write_text(json.dumps({'serial': 'SCUS-94900'}))
                if '-Players' in argv:
                    project = Path(argv[argv.index('-Dir') + 1]) / 'Fixture'
                    project.mkdir()
                    (project / 'game.toml').write_text('[game]\nplayers=1\n')
                if 'generate' in argv:
                    raise RuntimeError('stop before compiler')
                return ''
            with mock.patch.object(backend.shutil, 'which', return_value='pwsh'), \
                 mock.patch.object(backend, 'snapshot_source', side_effect=lambda kind, *args: {'kind': kind}), \
                 mock.patch.object(backend.buildtools, 'prepare', return_value={
                     'receipt': {}, 'environment': os.environ.copy(), 'module': 'fixture', 'sdl_prefix': 'fixture'}), \
                 mock.patch.object(runner, 'run', side_effect=run):
                with self.assertRaisesRegex(RuntimeError, 'stop before compiler'):
                    backend.build(options, root / 'data', runner)
            setup = next(command for command in commands if '-Players' in command)
            refresh = next(command for command in commands if '--write-game-toml' in command)
            self.assertEqual(setup[setup.index('-Players') + 1], '1')
            self.assertEqual(refresh[refresh.index('--players') + 1], '1')


if __name__ == '__main__':
    unittest.main()
