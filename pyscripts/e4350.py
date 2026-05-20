#!/usr/bin/python3
"""Driver for the Agilent/Keysight E4350-family Solar Array Simulator.

Talks to the instrument over a Prologix GPIB-USB controller. The original
driver (commit 7d2a7ab and earlier) was written for a single E4350A and is
the reference for SCPI-line shape. This module keeps that shape but adds:

  * Prologix auto-config (++mode/++addr/++auto/++eoi/++eos).
  * `*CLS` at connect-time so stale errors from a prior session don't get
    misattributed to the current run.
  * Error-queue drain helper used between logical operations, instead of
    polling `SYST:ERR?` after every single SET (the per-line polling pattern
    is what caused the -350 "Too many errors" cascades in May 2026).
  * Atomic SAS-curve SET: the four SAS parameters are sent as one compound
    SCPI line so the firmware validates them together (sending them
    individually leaves transient invalid states the device rejects).
  * Client-side range checks with messages that explain what to try next.
"""
import argparse
import math
import re
import serial
import sys
import time
import subprocess

from pyscripts.pvcells import PVCell


class E4350Exception(Exception):
    pass


# (Isc max [A], Voc max [V]) by IDN-reported model / option, from the
# Agilent E4350B/E4351B datasheet (Power Products Catalog 2002-2003, p.69-70).
# The model name is what *IDN? reports after the vendor; J-codes appear
# either in the IDN tail or as a separate option string on some firmware.
DEVICE_LIMITS = {
    'E4350B':     (8.0,  65.0),
    'E4351B':     (4.0,  130.0),
    'E4350A':     (8.0,  65.0),
    'E4350':      (8.0,  65.0),
    'E4350B-J01': (9.6,  54.0),
    'E4350B-J02': (6.0,  86.6),
    'E4350B-J03': (10.0, 52.0),
    'E4350B-J04': (11.0, 47.0),
    'E4350B-J06': (7.0,  74.0),
}

_BENIGN_ERR_PREFIXES = ('+0', '-420')   # +0 = no error, -420 = Query UNTERMINATED


class E4350:
    def __init__(self, ser, addr, debug=False, fake=False, cautious=False):
        self.cmax, self.vmax = 8.0, 65.0       # E4350B defaults; refined below
        self.ser, self.addr = ser, addr
        self.debug, self.fake, self.cautious = debug, fake, cautious
        self.prologix_auto = True
        self.model = 'E4350'

        if fake:
            self.model = 'E4350-FAKE'
            return

        # Prologix controller setup. Keep these in order — `++auto 1` must
        # come after `++addr` so the auto-read targets the right instrument.
        for cmd in ('++mode 1',
                    '++addr {}'.format(addr),
                    '++eoi 1',
                    '++auto 1',
                    '++eos 3'):
            self._raw_write(cmd)
            self._read_settle(0.1)
            m = re.match(r'\+\+auto\s+(\d+)', cmd)
            if m:
                self.prologix_auto = (m.group(1) == '1')

        # Clear any stale errors / status from a prior session BEFORE doing
        # anything that might itself cause an error. *CLS does not change
        # operating parameters; it only resets status registers and the
        # error queue.
        self._raw_write('*CLS')
        self._read_settle(0.1)
        self._drain_errors(max_iters=30)

        # Identify the instrument.
        resp = self.query('*IDN?')
        if not resp:
            raise E4350Exception(
                'no response to *IDN? — check Prologix port, GPIB address, '
                'and that the instrument is powered on')
        idn = [p.strip().upper() for p in resp.split(',')]
        vendor = idn[0] if idn else ''
        model_raw = idn[1] if len(idn) > 1 else ''
        if not (('HEWLETT' in vendor) or ('AGILENT' in vendor)
                or ('KEYSIGHT' in vendor)
                or 'E4350' in resp.upper() or 'E4351' in resp.upper()):
            raise E4350Exception('not an E4350-family instrument: ' + str(resp))
        self.model = model_raw or 'E4350'

        # Detect J-option from the IDN tail. (Not all units include it there;
        # absence just means we fall back to the bare-model limits.)
        opt = None
        m = re.search(r'J\d{2}', resp.upper())
        if m:
            opt = m.group(0)

        key = f'{self.model}-{opt}' if opt and f'{self.model}-{opt}' in DEVICE_LIMITS \
            else self.model
        if key in DEVICE_LIMITS:
            self.cmax, self.vmax = DEVICE_LIMITS[key]
        self.variant = key

        # Use the instrument's current SAS readbacks as a *floor* on cmax/vmax —
        # if the device already accepts a larger value, we should too. (Some
        # units run slightly above the rated max; ours readbacks Isc=8.16 A.)
        try:
            voc_rb = float(self.query('SOUR:VOLT:SAS:VOC?'))
            isc_rb = float(self.query('SOUR:CURR:SAS:ISC?'))
            if isc_rb > self.cmax:
                self.cmax = isc_rb
            if voc_rb > self.vmax:
                self.vmax = voc_rb
        except (ValueError, E4350Exception):
            pass

    # ---- transport ------------------------------------------------------

    def _raw_write(self, msg):
        if self.fake:
            if self.debug:
                print(msg)
            return
        if self.debug:
            print(msg)
        self.ser.write(bytes(msg + '\n', 'ascii'))

    def _read_settle(self, settle=0.3):
        if self.fake:
            return ''
        time.sleep(settle)
        data = self.ser.read_all().decode(errors='replace')
        if self.debug and data:
            print('>> ' + data.rstrip())
        return data

    def _read_response(self, settle=0.3, retries=2):
        """Read after a query. Falls back to ++read eoi if auto-read is off."""
        out = self._read_settle(settle)
        if out.strip() == '' and not self.prologix_auto:
            self._raw_write('++read eoi')
            out = self._read_settle(settle)
        attempt = 0
        while out.strip() == '' and attempt < retries:
            time.sleep(0.2)
            out = self._read_settle(settle)
            attempt += 1
        return out.strip()

    # ---- queries / sets / error queue -----------------------------------

    def query(self, scpi):
        """Send a SCPI query and return the trimmed response."""
        if self.fake:
            self._raw_write(scpi)
            return ''
        self._raw_write(scpi)
        return self._read_response()

    def _drain_errors(self, max_iters=10):
        """Pop the SCPI error queue until empty. Returns the non-benign errors."""
        if self.fake:
            return []
        errs = []
        for _ in range(max_iters):
            self._raw_write('SYST:ERR?')
            line = self._read_response(settle=0.2, retries=1)
            if not line:
                break
            if any(line.startswith(p) for p in _BENIGN_ERR_PREFIXES) \
                    or 'No error' in line:
                break
            errs.append(line)
        return errs

    def check_errors(self, context=''):
        """Pop the error queue. Raise if anything non-benign is present."""
        if self.fake:
            return
        errs = self._drain_errors()
        if errs:
            ctx = f' during {context}' if context else ''
            raise E4350Exception(f'instrument reported errors{ctx}: '
                                 + '; '.join(errs))

    # Backwards-compatible wrapper for callers that used .send() directly
    # (talke4530 and any user scripts). Routes queries to query() and
    # writes to a plain write + drain.
    def send(self, msg):
        if msg.startswith('++'):
            self._raw_write(msg)
            return self._read_settle(0.15)
        if msg.strip().endswith('?'):
            return self.query(msg)
        self._raw_write(msg)
        self._read_settle(0.15)
        return ''

    # ---- high-level operations ------------------------------------------

    def output_off(self):
        if self.fake:
            self._raw_write('OUTP OFF')
            return
        self._raw_write('OUTP OFF')
        self._read_settle(0.15)
        if self.cautious:
            self.check_errors('output_off')

    def output_on(self):
        self._raw_write('OUTP ON')
        self._read_settle(0.15)
        if self.cautious:
            self.check_errors('output_on')

    def pt_mode(self):
        self._raw_write('SOUR:CURR:MODE TABL')
        self._read_settle(0.15)
        if self.cautious:
            self.check_errors('pt_mode')

    def sim_mode(self):
        self._raw_write('SOUR:CURR:MODE SAS')
        self._read_settle(0.15)
        if self.cautious:
            self.check_errors('sim_mode')

    def set_protection(self, voltage=None, current=None):
        if voltage is not None:
            self._raw_write('SOUR:VOLT:PROT {}'.format(min(voltage, self.vmax)))
            self._read_settle(0.15)
        if current is not None:
            self._raw_write('SOUR:CURR:PROT {}'.format(min(current, self.cmax)))
            self._read_settle(0.15)
        if self.cautious:
            self.check_errors('set_protection')

    def sim_pts(self, isc, vmp, imp, voc):
        """Configure the four SAS-curve parameters atomically.

        Sent as one compound SCPI line — the firmware validates the four
        coupled parameters together. Splitting this into four separate
        commands lets the device see intermediate states (e.g. new Voc=20 V
        while the still-stored Vmp=49 V is in place) and reject them.
        """
        try:
            isc = float(isc); vmp = float(vmp); imp = float(imp); voc = float(voc)
        except (TypeError, ValueError) as e:
            raise E4350Exception(f'non-numeric SAS parameter: {e}')

        # Client-side checks. These catch the most common foot-guns and give
        # an actionable message before the instrument's terse -222 response.
        if min(isc, vmp, imp, voc) <= 0:
            raise E4350Exception(
                f'SAS parameters must all be positive: '
                f'isc={isc}, vmp={vmp}, imp={imp}, voc={voc}')
        if vmp >= voc:
            raise E4350Exception(f'Vmp ({vmp} V) must be < Voc ({voc} V)')
        if imp >= isc:
            raise E4350Exception(f'Imp ({imp} A) must be < Isc ({isc} A)')
        if voc > self.vmax or vmp > self.vmax:
            raise E4350Exception(
                f'voltage exceeds device max ({self.vmax} V): '
                f'vmp={vmp}, voc={voc}')
        if isc > self.cmax or imp > self.cmax:
            raise E4350Exception(
                f'current exceeds device max ({self.cmax} A): '
                f'isc={isc}, imp={imp}')
        # Heuristic minimum. The E4350 family rejects very small Vmp values
        # (the failure mode that confused us in May 2026 — `--sim=...,0.7,...`
        # was interpreted as 0.7 V absolute, below the firmware minimum).
        # `--sim` takes amps and volts, NOT fractions of full scale.
        if not self.fake and (voc < 1.0 or vmp < 1.0):
            raise E4350Exception(
                f'Vmp/Voc below 1 V (vmp={vmp}, voc={voc}). --sim takes '
                f'absolute amps and volts, not fractions. For an 8s CubeSat '
                f'string of triple-junction cells try Vmp~18, Voc~21.')

        cmd = ('SOUR:CURR:SAS:ISC {isc};IMP {imp};'
               ':SOUR:VOLT:SAS:VOC {voc};VMP {vmp}').format(
                   isc=isc, imp=imp, voc=voc, vmp=vmp)
        self._raw_write(cmd)
        self._read_settle(0.2)
        if self.cautious:
            self.check_errors(
                f'sim_pts(isc={isc}, vmp={vmp}, imp={imp}, voc={voc})')

    def set_pts(self, ptlist):
        # ptlist is [(float, float)...] of v,i points
        self._raw_write('MEM:TABL:SEL foobar')   # de-select 'frompython'
        self._read_settle(0.15)
        # Try to delete a previous 'frompython' table; -141 means it never
        # existed, which is fine. We swallow only that one error.
        self._raw_write('MEM:DEL:NAME frompython')
        self._read_settle(0.15)
        if self.cautious:
            try:
                self.check_errors('MEM:DEL:NAME')
            except E4350Exception as e:
                if '-141' not in str(e):
                    raise

        self._raw_write('MEM:TABL:SEL frompython')
        self._read_settle(0.15)

        ptlist.sort(key=lambda vi: vi[0])
        trimpts, vpt, n, acc = [], int(100 * ptlist[0][0]), 1, ptlist[0][1]
        for v, i in ptlist[1:]:
            v = int(100 * v)
            if v != vpt:
                trimpts.append((vpt * 0.01, acc / n))
                vpt, n, acc = v, 0, 0
            n += 1
            acc += i
        trimpts.append((vpt * 0.01, acc / n))

        for n in range(0, math.ceil(len(trimpts) / 100)):
            pts = trimpts[n * 100:(n + 1) * 100]
            self._raw_write('MEM:TABL:VOLT ' + ','.join(
                '{:.3f}'.format(v) for v, _ in pts))
            self._read_settle(0.15)
            self._raw_write('MEM:TABL:CURR ' + ','.join(
                '{:.3f}'.format(i) for _, i in pts))
            self._read_settle(0.15)
        self._raw_write('CURR:TABL:NAME frompython')
        self._read_settle(0.15)
        if self.cautious:
            self.check_errors('set_pts')

    def get_telem(self):
        if self.fake:
            self.query('MEAS:VOLT?')
            self.query('MEAS:CURR?')
            return {'v': 3.1415, 'i': 2.71828}
        return {'v': float(self.query('MEAS:VOLT?')),
                'i': float(self.query('MEAS:CURR?'))}

    def set_display(self, txt=None):
        if txt is None:
            self._raw_write('DISP:MODE NORM')
            self._read_settle(0.15)
        else:
            self._raw_write("DISP:TEXT '{}'".format(str(txt).upper()[:15]))
            self._read_settle(0.15)
            self._raw_write('DISP:MODE TEXT')
            self._read_settle(0.15)


# ---- CLI ---------------------------------------------------------------

def _autodetect(verbose=False):
    """Return (port, address, idn) by running the prologix-scan helper."""
    scan = None
    last_err = None
    for cmd in (['uv', 'run', 'prologix-scan'],
                [sys.executable, '-m', 'pyscripts.prologix_scan']):
        try:
            scan = subprocess.run(cmd, capture_output=True, text=True)
            break
        except FileNotFoundError as e:
            last_err = e
            continue
    if scan is None:
        raise E4350Exception(f'could not run prologix-scan: {last_err}')
    if scan.returncode != 0:
        stderr = (scan.stderr or '').strip()
        stdout = (scan.stdout or '').strip()
        if 'Permission denied' in stderr or 'could not open port' in stderr:
            raise E4350Exception(
                'prologix-scan failed (permission denied). Run with sudo or '
                'add your user to the dialout group.\n' + stderr)
        raise E4350Exception(
            'prologix-scan failed: ' + (stderr or stdout or 'unknown error'))

    port = address = idn = None
    for line in (scan.stdout or '').splitlines():
        line = line.strip()
        if line.lower().startswith('port:') and port is None:
            port = line.split(':', 1)[1].strip()
        m = re.match(r'address\s+(\d+):\s*(.+)', line, re.I)
        if m:
            a, found = m.group(1), m.group(2).strip()
            if 'E4350' in found.upper() or 'E4351' in found.upper():
                address, idn = a, found
                break
    if verbose and idn:
        print(f'Auto-detected instrument at address {address}: {idn}')
    return port, address, idn


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Drive an Agilent/Keysight E4350-family Solar Array Simulator.')
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('-n', '--nohardware', action='store_true',
                        help='dry run; do not talk to an instrument')
    parser.add_argument('-p', '--port',
                        help='Prologix serial port (auto-detected if omitted)')
    parser.add_argument('-a', '--address',
                        help='E4350 GPIB address (auto-detected if omitted)')
    parser.add_argument('-f', '--infile',
                        help='IV curve CSV ("V_in_Volts,I_in_A" per line)')
    parser.add_argument('--calc',
                        help='compute an IV curve as --calc=Isc,Vmp,Imp,Voc,a')
    parser.add_argument('--sim',
                        help='SAS curve as absolute values: '
                             '--sim=Isc[A],Vmp[V],Imp[A],Voc[V]')
    parser.add_argument('--multiple', default='1s1p',
                        help='array layout, e.g. 8s2p (8 in series, 2 in parallel)')
    parser.add_argument('-t', '--period', type=float, default=5,
                        help='telemetry log period (seconds)')
    parser.add_argument('--max-voltage', type=float,
                        help='override detected device Voc max (V)')
    parser.add_argument('--max-current', type=float,
                        help='override detected device Isc max (A)')
    args = parser.parse_args(argv)

    mutex = (args.infile, args.sim, args.calc)
    if sum(a is not None for a in mutex) != 1:
        print('error: provide exactly one of --sim / --infile / --calc',
              file=sys.stderr)
        return 2

    if not args.nohardware and (args.port is None or args.address is None):
        port, addr, _ = _autodetect(verbose=args.verbose)
        if args.port is None:
            args.port = port
        if args.address is None:
            args.address = addr
        if args.port is None or args.address is None:
            raise E4350Exception(
                'could not auto-detect Prologix port and/or E4350 GPIB '
                'address; specify --port and --address')

    if args.nohardware:
        sas = E4350(None, args.address or 0, debug=args.verbose, fake=True)
    else:
        ser = serial.Serial(args.port, 9600, timeout=1)
        sas = E4350(ser, args.address, debug=args.verbose, cautious=True)

    if args.max_voltage is not None:
        sas.vmax = float(args.max_voltage)
    if args.max_current is not None:
        sas.cmax = float(args.max_current)

    sas.output_off()

    m = re.match(r'((\d*)[Ss])?((\d*)[Pp])?', args.multiple)
    _, series, _, parallel = m.groups()
    if series is None and parallel is None:
        raise E4350Exception('--multiple must be NsMp form, e.g. 8s2p')
    series = int(series) if series else 1
    parallel = int(parallel) if parallel else 1

    if args.sim:
        try:
            isc, vmp, imp, voc = [float(s) for s in args.sim.split(',')]
        except ValueError as e:
            raise E4350Exception(f'--sim must be Isc,Vmp,Imp,Voc: {e}')
        try:
            sas.sim_mode()
            sas.sim_pts(isc * parallel, vmp * series,
                        imp * parallel, voc * series)
            sas.output_on()
        except Exception:
            sas.output_off()
            raise
    else:
        if args.infile:
            with open(args.infile) as f:
                rows = [ln.strip().split(',') for ln in f
                        if ln.strip() and not ln.startswith('#')]
                iv = [(float(v.strip()), float(i.strip())) for v, i in rows]
        else:
            isc, vmp, imp, voc, a = [float(s) for s in args.calc.split(',')]
            pvcell = PVCell(voc, vmp, imp, isc, a)
            data = pvcell.iv_curve()

            def _sigint(_sig, _frame):
                raise KeyboardInterrupt()
            import signal
            signal.signal(signal.SIGINT, _sigint)

            iv = list(zip(data['vout'], data['aout']))
            while len(iv) > 4000:
                iv = [iv[n] for n in range(0, len(iv), 2)]

        ivcurve = [(v * series, i * parallel) for v, i in iv]
        try:
            sas._raw_write('CURR:MODE FIX')
            sas._read_settle(0.15)
            if sas.cautious:
                sas.check_errors('CURR:MODE FIX')
            sas.set_pts(ivcurve)
            sas.pt_mode()
            sas.set_protection(max(v for v, _ in ivcurve) * 1.1,
                               max(i for _, i in ivcurve) * 1.1)
            sas.output_on()
        except Exception:
            sas.output_off()
            raise

    import signal

    def _shutdown(_sig, _frame):
        sas.output_off()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)

    try:
        while True:
            t0 = time.time()
            dat = sas.get_telem()
            print('{:.3f}: {:.3f} V  {:.3f} A'.format(
                time.time(), dat['v'], dat['i']))
            time.sleep(max(0, args.period - (time.time() - t0)))
    except KeyboardInterrupt:
        sas.output_off()
        print('')
    except Exception:
        sas.output_off()
        raise
    return 0


if __name__ == '__main__':
    sys.exit(main())
