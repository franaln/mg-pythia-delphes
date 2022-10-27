#! /usr/bin/env python3

import os
import argparse
import math
import glob

M_W = 80.4
M_top = 173.0
m_h = 125.0
res_mh = 0.1

# ---------
# Functions
# ---------

def dot(p1, p2):
    return p1.e*p2.e - p1.px*p2.px - p1.py*p2.py - p1.pz*p2.pz

def get_invmass(p1, p2):
    ptot = p1 + p2
    return math.sqrt(dot(ptot, ptot))

def get_inv3mass(p1, p2, p3):
    ptot = p1 + p2 + p3
    return math.sqrt(dot(ptot, ptot))

def get_inv4mass(p1, p2, p3, p4):
    ptot = p1 + p2 + p3 + p4
    return math.sqrt(dot(ptot, ptot))


def defangle(a):
    """
     return phi in the interval [-pi, pi]
    """
    if a > math.pi:
        return a - 2*math.pi
    elif a < -math.pi:
        return a + 2*math.pi

    return a


def get_dphi(p1, p2):

    if isinstance(p1, FourVector):
        p1 = p1.phi
    if isinstance(p2, FourVector):
        p2 = p2.phi

    return defangle(p1 - p2)


def get_deta(p1, p2):

    if isinstance(p1, FourVector):
        p1 = p1.eta
    if isinstance(p2, FourVector):
        p2 = p2.eta

    return p1 - p2

def get_dR(p1, p2):
    deta = get_deta(p1, p2)
    dphi = get_dphi(p1, p2)
    return math.sqrt(deta**2 + dphi**2)

def get_chiHH(b1, b2, b3, b4):

    m12 = get_invmass(b1, b2)
    m34 = get_invmass(b3, b4)

    ### CHECK: different definition btw paper and code?
    # a12 = (m_h - m12) / (res_mh * m_h)
    # a34 = (m_h - m34) / (res_mh * m_h)

    a12 = (m_h - m12) / (res_mh * m12)
    a34 = (m_h - m34) / (res_mh * m34)

    return math.sqrt( a12**2 + a34**2 )



class FourVector:

    def __init__(self, e, px, py, pz):
        self.e = e
        self.px = px
        self.py = py
        self.pz = pz

        self.pt = math.sqrt(self.px**2 + self.py**2)
        self.pabs = math.sqrt(self.px**2 + self.py**2 + self.pz**2)
        self.eta = math.log((self.pabs + self.pz)/(self.pabs - self.pz)) * 0.5
        self.phi = math.atan2(self.py, self.px)

    def __getitem__(self, idx):
        p = [self.e, self.px, self.py, self.pz]
        return p[idx]

    def __add__(self, o):
        e = self.e + o.e
        px = self.px + o.px
        py = self.py + o.py
        pz = self.pz + o.pz
        return FourVector(e, px, py, pz)


class Object:

    def __init__(self, typ, eta, phi, pt, mass, ntrk):
        self.typ = typ
        self.eta = eta
        self.phi = phi
        self.pt = pt
        self.mass = mass
        self.ntrk = ntrk

        # only for leptons
        self.charge = +1 if ntrk > 0 else -1

        # four-vector
        px = pt * math.cos(phi)
        py = pt * math.sin(phi)
        pz = pt * math.sinh(eta)
        e = math.sqrt(px*px + py*py + pz*pz + mass*mass)

        self.p = FourVector(e, px, py, pz)


class Event:

    def __init__(self):

        self.photons = []
        self.leptons = []
        self.jets    = []
        self.ljets   = []
        self.bjets   = []
        self.taus    = []

        self.met_et  = -999.
        self.met_phi = -999.
        self.met_ex  = -999.
        self.met_ey  = -999.

        # selection/cutflow
        self.good = 0

        # extra variables
        self.ht      = -999.
        self.met_sig = -999.

        self.dphi_met_b1 = -999.
        self.dphi_met_b2 = -999.
        self.dphi_met_b3 = -999.
        self.dphi_met_b4 = -999.

        #self.chiHH = []
        self.chiHH_min = -999.

        self.pH1 = None
        self.pH2 = None

        self.dphi_met_H1 = -999.
        self.dphi_met_H2 = -999.


def process_event(lines):

    event = Event()

    # -------
    # Objects
    # -------
    for line in lines:

        n, typ, eta, phi, pt, jmass, ntrk, btag, hadem, _, _ = line.split()

        n = int(n)
        typ = int(typ)
        eta = float(eta)
        phi = float(phi)
        pt = float(pt)
        jmass = float(jmass)
        ntrk = int(float(ntrk))
        btag = int(float(btag))

        obj = Object(typ, eta, phi, pt, jmass, ntrk)

        # photon
        if typ == 0:
            event.photons.append(obj)

        # jets (light and b-jets)
        elif typ == 4:
            event.jets.append(obj)

            # light-jets
            if btag == 0:
                event.ljets.append(obj)

            # b-jets
            else:
                event.bjets.append(obj)

        # leptons
        elif typ == 1 or typ == 2:
            event.leptons.append(obj)

        # hadronic taus
        elif typ == 3:
            event.taus.append(obj)

        # MET
        elif typ == 6:
            event.met_et = pt
            event.met_px = pt * math.cos(phi)
            event.met_py = pt * math.sin(phi)
            event.met_phi = phi


    # ---------------
    # Event variables
    # ---------------
    n_jets    = len(event.jets)
    n_bjets   = len(event.bjets)
    n_leptons = len(event.leptons)
    n_taus    = len(event.taus)
    n_photons = len(event.photons)

    # MET significance. CHECK: sum only bjets pt or include light jets?
    sum_pt = 0
    for i in range(n_bjets):
        sum_pt += event.bjets[i].pt

    if sum_pt > 0:
        event.met_sig = event.met_et / math.sqrt(sum_pt)

    # definimos las high features que involucran la met con los b-jets
    if n_bjets > 0:
        event.dphi_met_b1 = get_dphi(event.met_phi, event.bjets[0].phi)
    if n_bjets > 1:
        event.dphi_met_b2 = get_dphi(event.met_phi, event.bjets[1].phi)
    if n_bjets > 2:
        event.dphi_met_b3 = get_dphi(event.met_phi, event.bjets[2].phi)
    if n_bjets > 3:
        event.dphi_met_b4 = get_dphi(event.met_phi, event.bjets[3].phi)


    #   definimos el chi_hh (definición de atlas) para el decaimiento de
    #   dos h (mh=125GeV con resolucion de masa del 10%) a cuatro b-jets
    #   junto con las variables asociadas a los Higgs reconstruidos. Esto
    #   sólo funciona para 4 b-jets: si Nb>=5, hay que definir un contador
    #   para tener control sobre las cuaternas de b-jets e ir actualizando
    #   el minchiHH según se recorran las cuaternas. Además la selección del
    #   1st y 2nd leading H (que corresponden al minchiHH) es muy fuerza
    #   bruta y lo ideal sería optimizarlo inteligentemente…
    if n_bjets == 4:

        pH_01 = event.bjets[0].p + event.bjets[1].p
        pH_23 = event.bjets[2].p + event.bjets[3].p

        pH_02 = event.bjets[0].p + event.bjets[2].p
        pH_13 = event.bjets[1].p + event.bjets[3].p

        pH_03 = event.bjets[0].p + event.bjets[3].p
        pH_12 = event.bjets[1].p + event.bjets[2].p

        # all possible bjets combinations
        chiHH = [
            get_chiHH(event.bjets[0].p, event.bjets[1].p, event.bjets[2].p, event.bjets[3].p),
            get_chiHH(event.bjets[0].p, event.bjets[2].p, event.bjets[1].p, event.bjets[3].p),
            get_chiHH(event.bjets[0].p, event.bjets[3].p, event.bjets[1].p, event.bjets[2].p),
        ]

        chiHH_min = min(chiHH)
        chiHH_min_idx = min(range(len(chiHH)), key=lambda x : chiHH[x])

        # save H1 (leading) and H2 (subleading) momentums with min chiHH
        if chiHH_min_idx == 0:
            if pH_01.pt >= pH_23.pt:
                event.pH1 = pH_01
                event.pH2 = pH_23
            else:
                event.pH1 = pH_23
                event.pH2 = pH_01

        elif chiHH_min_idx == 1:
            if pH_02.pt >=  pH_13.pt:
                event.pH1 = pH_02
                event.pH2 = pH_13
            else:
                event.pH1 = pH_13
                event.pH2 = pH_02

        elif chiHH_min_idx == 2:
            if pH_03.pt > pH_12.pt:
                event.pH1 = pH_03
                event.pH2 = pH_12
            else:
                event.pH1 = pH_12
                event.pH2 = pH_03

        event.chiHH_min = chiHH_min

        # high-level features involving reconstructed H
        event.HH_mass = get_invmass(event.pH1, event.pH2)
        event.HH_deta = get_deta(event.pH1.eta, event.pH2.eta)
        event.HH_dphi = get_dphi(event.pH1.phi, event.pH2.phi)
        event.HH_dR   = get_dR(event.pH1, event.pH2)

        event.dphi_met_H1 = get_dphi(event.met_phi, event.pH1.phi)
        event.dphi_met_H2 = get_dphi(event.met_phi, event.pH2.phi)


    # --------
    # Cut-flow
    # --------

    # Cut definition
    # good=0 - no cut, all events
    # good=1 - nbjet=4, no leptons, no taus, MET>200, bjets pt > 20

    event.good = 0
    if n_bjets == 4 and n_leptons == 0 and n_taus == 0 and event.met_et > 200 and event.bjets[3].pt > 20:
        event.good += 1


    return event


def read_events_lhco(lhco_file):

    events = []
    current_event_lines = []

    lines = open(lhco_file).read().split('\n')

    for line in lines:

        line = line.strip()

        # skip empty or commented lines
        if not line or line.startswith("#"):
            continue

        # New event in file starts with "0"
        if line.startswith('0'):

            # process current event and clear for the next one
            if current_event_lines:
                event = process_event(current_event_lines)
                if event is not None:
                    events.append(event)

            # clear previous event and continue to next line
            current_event_lines = []
            continue

        else:
            current_event_lines.append(line)

    # save last event
    if current_event_lines:
        event = process_event(current_event_lines)
        if event is not None:
            events.append(event)

    return events



if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='read_lhco.py')

    parser.add_argument('inputs', nargs='*', help='Input lhco files (if input is a directory it will run over all lhco inside)')
    parser.add_argument('-o', '--output_file', required=True, help='Output file')
    parser.add_argument('-t', '--event_type', required=True, help='Event type (0 for bkg, 1 for signal)')
    parser.add_argument('-f', '--features', choices=['low', 'high', 'all'], default='all', help='Output features (low, high or all)')

    args = parser.parse_args()


    if len(args.inputs) == 1 and os.path.isdir(args.inputs[0]):
        lhco_files = glob.glob(f'{args.inputs[0]}/*.lhco')
        print(f'# Input       = found {len(lhco_files)} lhco files inside the directory {args.inputs[0]}')
    else:
        lhco_files = [ x for x in args.inputs if x.endswith('.lhco') ]
        print(f'# Input       = {len(lhco_files)} lhco files')

    event_type = args.event_type
    features_type = args.features
    output_file = args.output_file

    print(f'# Event type  = {event_type}')
    print(f'# Features    = {features_type}')
    print(f'# Output file = {output_file}')

    of = open(output_file, 'w')

    # Loop over lhco files
    for lhco_file in lhco_files:

        print(f'Reading {lhco_file}')

        events = read_events_lhco(lhco_file)

        events_total = len(events)
        events_good  = 0

        # Loop over events and save features to output file
        for event in events:

            # skip events not passing the selection (good == 1)
            if event.good == 0:
                continue

            events_good += 1

            features_low = [
                len(event.ljets), # CHECK: number of jets or light-jets?
                event.bjets[0].eta,
                event.bjets[0].phi,
                event.bjets[0].pt,
                event.bjets[1].eta,
                event.bjets[1].phi,
                event.bjets[1].pt,
                event.bjets[2].eta,
                event.bjets[2].phi,
                event.bjets[2].pt,
                event.bjets[3].eta,
                event.bjets[3].phi,
                event.bjets[3].pt,
                event.met_phi,
                event.met_et,
            ]

            features_high = [
                event.pH1.eta,
                event.pH1.phi,
                event.pH1.pt,
                event.pH2.eta,
                event.pH2.phi,
                event.pH2.pt,
                event.HH_mass,
                event.HH_deta,
                event.HH_dphi,
                event.HH_dR,
                event.met_sig,
                event.dphi_met_b1,
                event.dphi_met_b2,
                event.dphi_met_b3,
                event.dphi_met_b4,
                event.dphi_met_H1,
                event.dphi_met_H2,
                event.chiHH_min
            ]

            features_low_str  = ', '.join([ f'{feature:.5f}' for feature in features_low ])
            features_high_str = ', '.join([ f'{feature:.5f}' for feature in features_high ])

            # only low-level features:
            if features_type == 'low':
                out_str = f'{event_type}, {features_low_str}'
            # only high-level features:
            elif features_type == 'high':
                out_str = f'{event_type}, {features_high_str}'
            # low+high features
            else:
                out_str = f'{event_type}, {features_low_str}, {features_high_str}'

            of.write(f'{out_str}\n')


        # finished processing this lhco file
        print(f'Total events = {events_total}, Selected events = {events_good} ({float(events_good)/events_total:.2%})')


    # Done. Close output file
    of.close()
    print(f'Done. Output saved in {args.output_file}')
