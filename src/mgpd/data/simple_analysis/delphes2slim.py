#!/usr/bin/env python3
"""Convert Delphes ROOT output to the SimpleAnalysis slim ntuple format.

Writes the tree ``ntuple`` with every branch that SimpleAnalysis' SlimReader
binds unconditionally (checked against atlas-sa/simple-analysis, framework
d9f8c704, AnalysisBase 21.2.242), so the output can be run with
``simpleAnalysis -a <analyses> <output>``.

Conventions and approximations (not ATLAS working points):

- Units are GeV and radians, as stored by Delphes.
- ``mcWeights`` holds one entry, the nominal signed ``Event.Weight``, without
  cross-section or luminosity scaling (SimpleAnalysis uses index 0 by default).
- Electrons, muons and photons pass every ID/isolation bit; jets get the
  SimpleAnalysis ``GoodJet`` bits, plus every b-tag working point when
  ``Jet.BTag`` is set. Tau candidates are ``TauTag`` jets with all tau ID bits
  and the one/three-prong bit from ``NCharged``.
- MET is the Delphes ``MissingET`` minus every reconstructed muon (the packaged
  ATLAS card builds MET from calorimeter towers, where muons deposit nothing).
  Disable with ``--no-muon-met-correction`` for cards whose MET includes muons.
- ``sumet`` is ``ScalarHT.HT``, or, when the card does not write ScalarHT, the
  scalar pT sum of the output Jet, Electron, Photon and Muon collections (equal
  to ScalarHT for the packaged ATLAS card); ``genMET`` comes from ``GenMissingET`` when
  present; ``genHT`` is 0. PDF scalars and ``mcChannel`` (ProcessID) are copied
  from the Delphes ``Event`` record, which may contain zeros only.
- ``fatjet_*`` is filled from a ``FatJet`` branch when present, else empty.
  Truth ancestry (``*_motherID``) is 0. Analyses relying on fat jets, truth,
  track jets, B hadrons or displaced tracks are not supported by this output.

Requires ROOT (PyROOT) and Delphes, e.g. inside the mg-pythia-delphes image:

    source /mg_pythia_delphes/root/bin/thisroot.sh
    export DELPHES_PATH=/mg_pythia_delphes/Delphes
    export ROOT_INCLUDE_PATH=$DELPHES_PATH:$DELPHES_PATH/external
    python3 delphes2slim.py -i delphes_events.root -o events_SA.root

mgpd runs this file inside the pinned mg-pythia-delphes image for
``mgpd analyze --simple-analysis/--to-slim``; ``scripts/Delphes2SA.py`` is a
link to it for standalone use.
"""

import argparse
import json
import os
import sys
from array import array

import ROOT

# AnalysisJetID::GoodJet: LooseBadJet, TightBadJet, JVT50Jet, JVT59Jet,
# JVT120Jet, PFlowJet, JVTLoose, JVTMedium, JVTTight (bits 8-10, 12, 14-18).
GOOD_JET = 0x0007D700
# All MV2c20/MV2c10/DL1/DL1r working points (bits 0-7, 20-23).
BTAG_ALL = 0x00F000FF
# Every ID and isolation bit except AnalysisCommonID::NotBit.
ALL_ID = 0x7FFFFFFF
TAU_ALL = 0xFF
TAU_ONE_PRONG = 1 << 10
TAU_THREE_PRONG = 1 << 11
MUON_MASS_GEV = 0.1056583715

INT_SCALARS = ("Event", "mcChannel", "mcVetoCode", "susyChannel", "pdf_id1", "pdf_id2")
FLOAT_SCALARS = (
    "genMET", "genHT", "pdf_x1", "pdf_pdf1", "pdf_x2", "pdf_pdf2", "pdf_scale",
    "sumet", "met_pt", "met_phi",
)
COLLECTIONS = ("el", "mu", "tau", "ph", "jet", "fatjet")
REQUIRED_BRANCHES = ("Event", "MissingET", "Photon", "Electron", "Muon", "Jet")
MASSIVE = ("jet", "fatjet")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("-i", "--input", required=True, nargs="+",
                        help="Delphes ROOT file(s); all are chained in order")
    parser.add_argument("-o", "--output", required=True,
                        help="New output ROOT file (never overwritten)")
    parser.add_argument("--max-events", type=int, default=-1,
                        help="Convert at most N events (preview); default: all")
    parser.add_argument("--no-muon-met-correction", action="store_true",
                        help="Keep Delphes MissingET unchanged")
    parser.add_argument("--summary",
                        help="New JSON file with event count, sumw/sumw2 and branch presence")
    return parser.parse_args()


def load_delphes():
    delphes_path = os.environ.get("DELPHES_PATH")
    if not delphes_path:
        sys.exit("DELPHES_PATH is not set")
    ROOT.gInterpreter.Declare(f'#include "{delphes_path}/classes/DelphesClasses.h"')
    ROOT.gInterpreter.Declare(
        f'#include "{delphes_path}/external/ExRootAnalysis/ExRootTreeReader.h"'
    )
    if ROOT.gSystem.Load(f"{delphes_path}/libDelphes.so") < 0:
        sys.exit(f"cannot load {delphes_path}/libDelphes.so")


class SlimWriter:
    """Owns the output tree and its branch buffers."""

    def __init__(self, tree):
        self.scalars = {}
        for name in INT_SCALARS:
            self.scalars[name] = array("i", [0])
            tree.Branch(name, self.scalars[name], name + "/I")
        for name in FLOAT_SCALARS:
            self.scalars[name] = array("f", [0.0])
            tree.Branch(name, self.scalars[name], name + "/F")

        self.vectors = []
        self.weights = self._vector(tree, "mcWeights", "float")
        self.objects = {}
        for coll in COLLECTIONS:
            fields = {f: self._vector(tree, f"{coll}_{f}", "float") for f in ("pt", "eta", "phi")}
            for f in ("charge", "id", "motherID"):
                fields[f] = self._vector(tree, f"{coll}_{f}", "int")
            if coll in MASSIVE:
                fields["m"] = self._vector(tree, f"{coll}_m", "float")
            self.objects[coll] = fields

    def _vector(self, tree, name, kind):
        vec = ROOT.std.vector(kind)()
        tree.Branch(name, vec)
        self.vectors.append(vec)
        return vec

    def clear(self):
        for vec in self.vectors:
            vec.clear()
        for buf in self.scalars.values():
            buf[0] = 0

    def set(self, name, value):
        self.scalars[name][0] = value

    def add(self, coll, obj, obj_id, charge, mass=None):
        fields = self.objects[coll]
        fields["pt"].push_back(obj.PT)
        fields["eta"].push_back(obj.Eta)
        fields["phi"].push_back(obj.Phi)
        fields["charge"].push_back(int(charge))
        fields["id"].push_back(obj_id)
        fields["motherID"].push_back(0)  # truth ancestry not available
        if coll in MASSIVE:
            fields["m"].push_back(mass)


def jet_id(jet):
    return GOOD_JET | (BTAG_ALL if jet.BTag else 0)


def check_input(path):
    if not os.path.isfile(path):
        sys.exit(f"input file not found: {path}")
    handle = ROOT.TFile.Open(path, "READ")
    try:
        if not handle or handle.IsZombie():
            sys.exit(f"cannot open ROOT file: {path}")
        tree = handle.Get("Delphes")
        if not tree or not tree.InheritsFrom("TTree"):
            sys.exit(f"no Delphes tree in {path}")
    finally:
        if handle:
            handle.Close()


def main():
    args = parse_args()
    if os.path.exists(args.output):
        sys.exit(f"refusing to overwrite existing output: {args.output}")
    partial = args.output + ".partial"
    if os.path.exists(partial):
        sys.exit(f"refusing to overwrite existing partial output: {partial}")
    if args.summary and os.path.exists(args.summary):
        sys.exit(f"refusing to overwrite existing summary: {args.summary}")
    load_delphes()
    try:
        summary = convert(args, partial)
    except BaseException:
        if os.path.exists(partial):
            os.unlink(partial)
        raise
    # Publish only a complete file; os.link fails instead of replacing output.
    os.link(partial, args.output)
    os.unlink(partial)
    if args.summary:
        with open(args.summary, "x", encoding="utf-8") as stream:
            json.dump(summary, stream, indent=2, allow_nan=False)
            stream.write("\n")
    print(f"Published {args.output}")


def convert(args, partial):

    chain = ROOT.TChain("Delphes")
    for path in args.input:
        check_input(path)
        chain.Add(path)
    missing = [name for name in REQUIRED_BRANCHES if not chain.GetBranch(name)]
    if missing:
        sys.exit(f"missing required Delphes branches: {', '.join(missing)}")
    reader = ROOT.ExRootTreeReader(chain)
    n_events = reader.GetEntries()
    if args.max_events >= 0:
        n_events = min(n_events, args.max_events)

    def optional_branch(name):
        return reader.UseBranch(name) if chain.GetBranch(name) else None

    branch_event = reader.UseBranch("Event")
    branch_met = reader.UseBranch("MissingET")
    branch_ht = optional_branch("ScalarHT")
    branch_photon = reader.UseBranch("Photon")
    branch_electron = reader.UseBranch("Electron")
    branch_muon = reader.UseBranch("Muon")
    branch_jet = reader.UseBranch("Jet")
    branch_gen_met = optional_branch("GenMissingET")
    branch_fatjet = optional_branch("FatJet")

    out_file = ROOT.TFile(partial, "CREATE")
    if not out_file or out_file.IsZombie():
        sys.exit(f"cannot create {partial}")
    out_tree = ROOT.TTree("ntuple", "SimpleAnalysis slim format from Delphes")
    out_tree.SetDirectory(out_file)
    out = SlimWriter(out_tree)

    sumw = 0.0
    sumw2 = 0.0
    negative = 0
    print(f"Converting {n_events} events")
    for entry in range(n_events):
        out.clear()
        reader.ReadEntry(entry)

        event = branch_event.At(0)
        weight = float(event.Weight)
        sumw += weight
        sumw2 += weight * weight
        negative += weight < 0
        out.weights.push_back(weight)
        out.set("Event", int(event.Number))
        out.set("mcChannel", int(event.ProcessID))
        out.set("pdf_id1", int(event.ID1))
        out.set("pdf_id2", int(event.ID2))
        out.set("pdf_x1", event.X1)
        out.set("pdf_x2", event.X2)
        out.set("pdf_pdf1", event.PDF1)
        out.set("pdf_pdf2", event.PDF2)
        out.set("pdf_scale", event.ScalePDF)
        if branch_gen_met is not None and branch_gen_met.GetEntries() > 0:
            out.set("genMET", branch_gen_met.At(0).MET)

        object_ht = 0.0
        met = ROOT.TLorentzVector()
        met.SetPtEtaPhiM(branch_met.At(0).MET, 0.0, branch_met.At(0).Phi, 0.0)

        for idx in range(branch_electron.GetEntries()):
            electron = branch_electron.At(idx)
            out.add("el", electron, ALL_ID, electron.Charge)
            object_ht += electron.PT

        for idx in range(branch_muon.GetEntries()):
            muon = branch_muon.At(idx)
            out.add("mu", muon, ALL_ID, muon.Charge)
            object_ht += muon.PT
            if not args.no_muon_met_correction:
                muon_vec = ROOT.TLorentzVector()
                muon_vec.SetPtEtaPhiM(muon.PT, muon.Eta, muon.Phi, MUON_MASS_GEV)
                met -= muon_vec

        for idx in range(branch_photon.GetEntries()):
            photon = branch_photon.At(idx)
            out.add("ph", photon, ALL_ID, 0)
            object_ht += photon.PT

        for idx in range(branch_jet.GetEntries()):
            jet = branch_jet.At(idx)
            out.add("jet", jet, jet_id(jet), jet.Charge, jet.Mass)
            object_ht += jet.PT
            if jet.TauTag:
                tau_id = TAU_ALL
                if jet.NCharged == 1:
                    tau_id |= TAU_ONE_PRONG
                elif jet.NCharged == 3:
                    tau_id |= TAU_THREE_PRONG
                out.add("tau", jet, tau_id, jet.Charge)

        if branch_fatjet is not None:
            for idx in range(branch_fatjet.GetEntries()):
                fatjet = branch_fatjet.At(idx)
                out.add("fatjet", fatjet, jet_id(fatjet), fatjet.Charge, fatjet.Mass)

        out.set("sumet", branch_ht.At(0).HT if branch_ht is not None else object_ht)
        out.set("met_pt", met.Pt())
        out.set("met_phi", met.Phi())
        out_tree.Fill()

    if out_tree.GetEntries() != n_events:
        sys.exit(f"wrote {out_tree.GetEntries()} entries, expected {n_events}")
    if out_file.Write() <= 0:
        sys.exit(f"failed to write {partial}")
    out_file.Close()
    print(f"Converted {n_events} entries")
    print(f"sumw={sumw!r} sumw2={sumw2!r} fatjet_branch={branch_fatjet is not None}")
    return {
        "schema_version": 1,
        "tree": "ntuple",
        "input_entries": int(reader.GetEntries()),
        "entries": int(n_events),
        "limit_reached": n_events < reader.GetEntries(),
        "sumw": sumw,
        "sumw2": sumw2,
        "negative_weight_events": int(negative),
        "muon_met_correction": not args.no_muon_met_correction,
        "branches": {"FatJet": branch_fatjet is not None,
                     "GenMissingET": branch_gen_met is not None,
                     "ScalarHT": branch_ht is not None},
        "sumet_source": "ScalarHT.HT" if branch_ht is not None else "sum of output Jet/Electron/Photon/Muon PT",
        "root_version": ROOT.gROOT.GetVersion(),
    }


if __name__ == "__main__":
    main()
