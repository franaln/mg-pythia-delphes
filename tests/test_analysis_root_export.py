"""Round-trip TH1D bins/errors/flows from independently known signed fixtures."""
from importlib.util import find_spec
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mgpd.analysis import plan_analysis, run_analysis, render_analysis
from test_analysis_engine import analysis_config, document, event


@unittest.skipUnless(all(find_spec(p) for p in ('numpy', 'awkward', 'vector', 'uproot', 'matplotlib')),
                     'requires analysis extra')
class RootExportTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        import uproot
        self.np, self.uproot = np, uproot
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.input = self.root/'events.lhe'
        # Masses 20, 80, 100, 120, 140; signed weights, final edge and flows.
        self.input.write_text(document([event(3,10), event(2,40), event(-1,50), event(.5,60), event(4,70), event(0)]))
        self.config = analysis_config()
        self.config['histograms'] = [{'name':'mass_hist', 'variable':'m_ee', 'stage':'initial',
                                     'bins':[60,80,120], 'label':'m(ee) [GeV]'}]

    def test_raw_signed_bins_variances_entries_and_saved_export(self):
        output = self.root/'fresh'
        report = run_analysis(plan_analysis(self.input,self.config),output,export_root=True)
        with self.uproot.open(output/'histograms.root') as root:
            h = root['raw/mass_hist']
            self.assertEqual(h.classname,'TH1D')
            self.np.testing.assert_array_equal(h.axis().edges(),[60,80,120])
            self.np.testing.assert_allclose(h.values(flow=True),[3,0,1.5,4])
            self.np.testing.assert_allclose(h.variances(flow=True),[9,0,5.25,16])
            self.assertEqual(h.member('fEntries'),5)
            self.assertAlmostEqual(h.member('fTsumwx'),150.)
            metadata=json.loads(str(root['metadata']))
            self.assertEqual(metadata['histograms']['mass_hist']['missing_values'],1)
            self.assertEqual(metadata['histograms_json_sha256'],hashlib.sha256((output/'histograms.json').read_bytes()).hexdigest())
        artifact=next(a for a in report['artifact_metadata'] if a['path']=='histograms.root')
        self.assertEqual(artifact['sha256'],hashlib.sha256((output/'histograms.root').read_bytes()).hexdigest())
        before={p.name:p.read_bytes() for p in output.iterdir() if p.is_file()}
        self.input.unlink()
        replay=self.root/'replay'
        subprocess.run([sys.executable,'-m','mgpd','analyze','--from-results',str(output),
                        '--export-root','-o',str(replay)],check=True,capture_output=True)
        self.assertFalse((replay/'plots').exists())
        self.assertEqual(before,{p.name:p.read_bytes() for p in output.iterdir() if p.is_file()})
        with self.uproot.open(replay/'histograms.root') as root:
            self.np.testing.assert_allclose(root['raw/mass_hist'].variances(flow=True),[9,0,5.25,16])

    def test_normalization_keeps_raw_and_scales_sumw2_including_flows(self):
        self.input.write_text(self.input.read_text().replace('-4 1','-3 1'))
        self.config['input']['normalization']={'mode':'luminosity','weight_convention':'dimensionless',
            'cross_section_pb':2.,'sumw_generated':4.,'luminosity_pb_inverse':100.}
        output=self.root/'normalized'
        run_analysis(plan_analysis(self.input,self.config),output,export_root=True)
        with self.uproot.open(output/'histograms.root') as root:
            self.np.testing.assert_allclose(root['raw/mass_hist'].values(flow=True),[3,0,1.5,4])
            self.np.testing.assert_allclose(root['normalized/mass_hist'].values(flow=True),[150,0,75,200])
            self.np.testing.assert_allclose(root['normalized/mass_hist'].variances(flow=True),[22500,0,13125,40000])
            self.assertEqual(root['normalized/mass_hist'].member('fEntries'),5)

    def test_empty_negative_and_object_fill_histograms(self):
        self.input.write_text(document([event(-2,40),event(1,40)]))
        self.config['histograms'][0]['bins']=[60,90,120]
        self.config['variables']['electron_pts']='electrons.pt'
        self.config['histograms'].append({'name':'pts','variable':'electron_pts','stage':'initial',
                                          'fill':'objects','bins':[0,50,100]})
        self.config['histograms'].append({'name':'empty','variable':'m_ee','stage':'mass_window','bins':[0,10]})
        output=self.root/'signed'
        run_analysis(plan_analysis(self.input,self.config),output,export_root=True)
        with self.uproot.open(output/'histograms.root') as root:
            self.np.testing.assert_allclose(root['raw/mass_hist'].values(),[-1,0])
            self.np.testing.assert_allclose(root['raw/mass_hist'].variances(),[5,0])
            self.assertEqual(root['raw/pts'].member('fEntries'),4)
            self.np.testing.assert_allclose(root['raw/pts'].variances(),[10,0])
        self.input.write_text(document([]))
        empty=self.root/'no-events'
        run_analysis(plan_analysis(self.input,self.config),empty,export_root=True)
        with self.uproot.open(empty/'histograms.root') as root:
            self.assertEqual(root['raw/mass_hist'].member('fEntries'),0)
            self.np.testing.assert_array_equal(root['raw/mass_hist'].values(flow=True),[0,0,0,0])

    def test_export_failure_no_publication_and_existing_destination_refused(self):
        import mgpd.analysis.root_export
        output=self.root/'failed'
        with patch('mgpd.analysis.root_export.write_root_histograms',side_effect=ValueError('writer failed')):
            with self.assertRaisesRegex(ValueError,'writer failed'):
                run_analysis(plan_analysis(self.input,self.config),output,export_root=True)
        self.assertFalse(output.exists())
        self.assertFalse(list(self.root.glob('.mgpd-analysis-*')))
        output.mkdir();(output/'keep').write_text('keep')
        with self.assertRaises(FileExistsError):
            run_analysis(plan_analysis(self.input,self.config),output,export_root=True)
        self.assertEqual((output/'keep').read_text(),'keep')
        saved=self.root/'saved'
        run_analysis(plan_analysis(self.input,self.config),saved)
        with patch('mgpd.analysis.root_export.write_root_histograms',side_effect=ValueError('writer failed')):
            with self.assertRaisesRegex(ValueError,'writer failed'):
                render_analysis(saved,self.root/'failed-saved',plots=False,export_root=True)
        self.assertFalse((self.root/'failed-saved').exists())

    def test_root_only_export_does_not_load_matplotlib(self):
        saved=self.root/'saved'
        run_analysis(plan_analysis(self.input,self.config),saved)
        code='from mgpd.analysis import render_analysis; import sys; render_analysis(sys.argv[1],sys.argv[2],plots=False,export_root=True); assert "matplotlib" not in sys.modules'
        subprocess.run([sys.executable,'-c',code,str(saved),str(self.root/'no-matplotlib')],check=True,capture_output=True)

    def test_unrepresentable_root_statistics_fail_without_publication(self):
        self.config['histograms'][0]['bins']=[0.,1e200]
        output=self.root/'overflow'
        with self.assertRaisesRegex(ValueError,'ROOT bin-center statistics'):
            run_analysis(plan_analysis(self.input,self.config),output,export_root=True)
        self.assertFalse(output.exists())
