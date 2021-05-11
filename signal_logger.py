#!/usr/bin/env python3

import configparser
import math
import struct
import sys
import threading
import tkinter as tk
import tkinter.font as tf
from time import sleep, strftime

import serial
import pyaudio


DEFAULT_CONFIG = {
    "Global":{
        "sample_rate":"32000",
        "chunk_size":"32768",
        "log_without_gps":"false",
        "gps_port":"COM1",
        "gps_baud":"4800",
        "gps_bits":"8",
        "gps_parity":"N",
        "gps_stopbits":"1",
        "gps_tcp":"False",
        "gps_url":""
    },
    "__Default_Input":{
        "source":"Hi-Fi Cable",
        "channel":"-1"
    }
}


AUDIO_DEVICE = "hi-fi cable"
NO_INPUT = 0.0000433 # internal noise of VB-Cable, measured.
COLUMNS = 3 # number of columns per RF shim GUI
VERSION = 0x0200


# let GlobalParameters determine these in the future
SAMPLE_FORMAT = "f" # define endianness?
SAMPLE_SIZE = 4 # 32-bit floating point is 4 bytes per sample
PA_FORMAT = pyaudio.paFloat32


def meter(pwr, cal=-65.94, floor=-122, scale=3):
    if cal: pwr = pwr_conv(pwr, cal=cal)
    return "#" * max(int(pwr - floor) // scale, 1)

def pwr_conv(raw_pwr, cal=-65.94):
    if raw_pwr < 0.1*NO_INPUT: return -200.
    try: return 20*math.log10(raw_pwr)+cal
    except ValueError:
        return -200.

def get_audio_device(pa, name=None):
    if not name: name = AUDIO_DEVICE
    found_device = False
    for dev in range(pa.get_device_count()):
        dev_info = pa.get_device_info_by_index(dev)
        if dev_info["name"].casefold().startswith(name.casefold()) and \
           dev_info["maxInputChannels"]:
            found_device = True
            break
    if not found_device: return
    return dev


class GlobalParametersManager(object): # GPM
    """Handle parameters that need to be shared, like GPS position"""
    def __init__(self, serial_parameters, serial_url=False):
        self.cpi = serial_parameters # ComPortInfo
        self.comport_url = serial_url
        self.gps_running = False
        self.lat = 0.
        self.lon = 0.
        self.log_loc_override = False # enable to allow non-GPS logging

    def start_gps(self):
        if self.gps_running: self.stop_gps()
        self.gps_rdy = threading.Event()
        self.gps_rdy.set()
        self.gps_running = True
        res = self.reset_gps()
        self.gps_thread = threading.Thread(target=self.run_gps, daemon=True)
        self.gps_thread.start()
        return res

    def reset_gps(self):
        try:
            if not self.comport_url:
                self.gps_ser = serial.Serial(**self.cpi)
            else:
                # UNTESTED: pyserial lets us use tcp connections for serial
                self.gps_ser = serial.serial_for_url(self.comport_url,
                                                     **self.cpi)
        except serial.SerialException as e:
            print("GPS: Serial did not open! Error info:")
            print(e.args[0], end='\n\n')
            return False
        return True

    def stop_gps(self):
        self.gps_running = False
        try:
            self.gps_ser.close()
            print("GPS: Serial port released.")
        except: pass
        if hasattr(self, "gps_rdy"): self.gps_rdy.set()

    def get_gps(self):
        self.gps_rdy.wait()
        self.gps_rdy.clear()
        out = tuple([self.lat, self.lon])
        self.gps_rdy.set()
        return out

    def run_gps(self):
        if not hasattr(self, "gps_ser"):
            print("GPS: \
Running without GPS! Location data will not be collected.")
            self.gps_rdy.set()
            return
        while self.gps_running:
            try: l = self.gps_ser.readline()
            except:
                print("GPS: Device reset due to failure!")
                print(sys.exc_info())
                while self.gps_running:
                    sleep(1)
                    if self.reset_gps(): break
                    sleep(1)
                if self.gps_running: print("GPS: Device recovered!")
                continue
            # compute and verify checksum
            cs_idx = l.find(b"*")
            if cs_idx == -1:
                sleep(0.001)
                continue
            cs = 0
            for c in l[1:cs_idx]:
                cs = cs ^ c
            # string process
            l = l.decode("ascii", "ignore")
            cs_idx = (l.find("*"), l.find("\r"))
            if cs_idx[0] == -1 or cs_idx[1] == -1:
                sleep(0.001)
                print("GPS: Checksum Missing!")
                continue
            try:
                if int(l[cs_idx[0]+1:cs_idx[1]], 16) != cs:
                    print("GPS: Checksum Mismatch!")
                    continue
            except ValueError:
                print("GPS: Checksum could not parse for:\n'%s'" % l)
                continue
            if not l.startswith("$GPGGA,"): continue
            l = l.strip('\r\n').split(',')
            if not l[2]: continue
            lat = (int(l[2][:2])+float(l[2][2:])/60)*(-1 if l[3] == 'S' else 1)
            lon = (int(l[4][:3])+float(l[4][3:])/60)*(-1 if l[5] == 'W' else 1)
            self.gps_rdy.wait()
            self.gps_rdy.clear()
            self.lat = lat
            self.lon = lon
            self.gps_rdy.set()

class MultiParametersManager(object): # MPM
    def __init__(self, adev=None, ach=-1):
        # perhaps this should get a PyAudio instance from GPM?
        self.pa_running = False
        self.cal = -46.
        if adev == None: adev = AUDIO_DEVICE
        self.adev = adev
        self.ach = ach
        self.pa_s = 0
        self.pa_sl = []
        self.pa_chnls = 2
        self.pa_sr = 32000
        self.pa_cs = 32768

    def start_audio(self):
        if self.pa_running: self.stop_audio()
        self.pa_rdy = threading.Event()
        self.pa_rdy.set()
        self.pa = pyaudio.PyAudio()
        if isinstance(self.adev, str):
            self.pa_dev = get_audio_device(self.pa, self.adev)
        else: self.pa_dev = self.adev
        if self.pa_dev == None:
            raise IOError("No suitable audio device found!")
        self.pa_thread = threading.Thread(target=self.run_audio, daemon=True)
        self.pa_running = True
        self.pa_thread.start()

    def stop_audio(self):
        self.pa_running = False
        self.pa_rdy.set() # prevents hanging; we do the same in GPM

    def run_audio(self):
        adev = self.pa.open(format=PA_FORMAT,
                            channels=self.pa_chnls,
                            rate=self.pa_sr,
                            input=True,
                            input_device_index=self.pa_dev)
        while self.pa_running:
            data = adev.read(self.pa_cs)
            res = [list() for i in range(self.pa_chnls)]
            for i in range(0, len(data), 4):
                res[(i//SAMPLE_SIZE) % self.pa_chnls].append(\
                    struct.unpack(SAMPLE_FORMAT, data[i:i+SAMPLE_SIZE])[0])
            self.pa_rdy.wait()
            self.pa_rdy.clear()
            self.pa_s = 0
            self.pa_sl = [0 for i in range(len(res[0]))]
            if self.ach == -1:
                for i in range(len(res[0])):
                    sampled_pwr = (res[0][i]**2 + res[1][i]**2)**0.5
                    self.pa_sl[i] = pwr_conv(sampled_pwr, cal=self.cal)
            else:
                for i in range(len(res[0])):
                    sampled_pwr = abs(res[self.ach][i])
                    self.pa_sl[i] = pwr_conv(sampled_pwr, cal=self.cal)
            self.pa_sl = sorted(filter(lambda q:q > pwr_conv(NO_INPUT,
                                                             cal=self.cal),
                                       self.pa_sl))
            if self.pa_sl:
                self.pa_s = (sum(map(lambda q:q**2, self.pa_sl))\
                             /len(self.pa_sl))**0.5
            self.pa_rdy.set()
        adev.close()

    def get_cal(self):
        self.pa_rdy.wait()
        self.pa_rdy.clear()
        res = self.cal
        self.pa_rdy.set()
        return res

    def get_sig(self):
        self.pa_rdy.wait()
        self.pa_rdy.clear()
        res = self.pa_s
        self.pa_rdy.set()
        return res

    def get_samples(self):
        self.pa_rdy.wait()
        self.pa_rdy.clear()
        res = len(self.pa_sl)
        self.pa_rdy.set()
        return res

    def get_sig_at(self, pct):
        self.pa_rdy.wait()
        self.pa_rdy.clear()
        if len(self.pa_sl) < 50:
            self.pa_rdy.set()
            return None
        if isinstance(pct, list):
            res = []
            for p in pct:
                if p > 1: p /= 100.
                res.append(self.pa_sl[int(-p*len(self.pa_sl))])
        else:
            if pct > 1: pct /= 100.
            res = self.pa_sl[int(-pct*len(self.pa_sl))]
        self.pa_rdy.set()
        return res

    def cal_up(self):
        self.pa_rdy.wait()
        self.pa_rdy.clear()
        self.cal += 1
        res = self.cal
        self.pa_rdy.set()
        return res

    def cal_dn(self):
        self.pa_rdy.wait()
        self.pa_rdy.clear()
        self.cal -= 1
        res = self.cal
        self.pa_rdy.set()
        return res


class RFDataShim(object):
    def __init__(self, dm_cb, instance, adev, name=None, init_cal=None):
        self.running = True
        self.logging = False
        self.thread = None
        self.dm_cb = dm_cb
        self.instance = instance
        if isinstance(adev, (list, tuple)):
            self.adev = adev[0]
            if len(adev) >= 2:
                self.channel = adev[1]
            else: self.channel = -1 # I/Q
        else:
            self.adev = adev
            self.channel = -1
        if not name: self.name = "Instance %d" % (self.instance + 1)
        else: self.name = name
        self.init_cal = init_cal # allow calibration presets in config

    def add_into_window(self):
        i = self.instance*COLUMNS
        self.sv_cal = tk.StringVar(value="NaN")
        self.sv_pwr = tk.StringVar(value="NaN")
        self.sv_pwr15 = tk.StringVar(value="NaN----")
        self.sv_pwr50 = tk.StringVar(value="NaN----")
        self.sv_pwr70 = tk.StringVar(value="NaN----")
        self.sv_pwr83 = tk.StringVar(value="NaN----")
        self.sv_pwr87 = tk.StringVar(value="NaN----")
        self.sv_pwr95 = tk.StringVar(value="NaN----")
        self.sv_gps = tk.StringVar(value="NaN,NaN")
        self.iv_log = tk.IntVar(value=(1 if self.logging else 0))
        self.l_cal = tk.Label(self.dm_cb.w, textvariable=self.sv_cal, width=5)
        self.l_pwr = tk.Label(self.dm_cb.w, textvariable=self.sv_pwr, width=6,
                              font=tf.Font(size=32))
        self.l_pwr15 = tk.Label(self.dm_cb.w,
                                textvariable=self.sv_pwr15,
                                width=11)
        self.l_pwr50 = tk.Label(self.dm_cb.w,
                                textvariable=self.sv_pwr50,
                                width=11)
        self.l_pwr70 = tk.Label(self.dm_cb.w,
                                textvariable=self.sv_pwr70,
                                width=11)
        self.l_pwr83 = tk.Label(self.dm_cb.w,
                                textvariable=self.sv_pwr83,
                                width=11)
        self.l_pwr87 = tk.Label(self.dm_cb.w,
                                textvariable=self.sv_pwr87,
                                width=11)
        self.l_pwr95 = tk.Label(self.dm_cb.w,
                                textvariable=self.sv_pwr95,
                                width=11)
        self.l_gps = tk.Label(self.dm_cb.w,
                              textvariable=self.sv_gps,
                              width=18)
        self.b_cal_up = tk.Button(self.dm_cb.w,
                                  command=self.cal_up,
                                  text="Cal+")
        self.b_cal_dn = tk.Button(self.dm_cb.w,
                                  command=self.cal_dn,
                                  text="Cal-")
        self.c_log = tk.Checkbutton(self.dm_cb.w,
                                    variable=self.iv_log,
                                    text="Enable Logging", 
                                    onvalue=1, offvalue=0)
        tk.Label(self.dm_cb.w, text=self.name, font=tf.Font(size=16))\
                               .grid(row=0, column=0+i, columnspan=COLUMNS)
        self.b_cal_dn.grid(row=1, column=0+i)
        self.l_cal.grid(row=1, column=1+i)
        self.b_cal_up.grid(row=1, column=2+i)
        self.l_pwr.grid(row=2, column=0+i, columnspan=3)
        tk.Label(self.dm_cb.w, text="15%", width=5).grid(row=3, column=0+i)
        self.l_pwr15.grid(row=3, column=1+i, columnspan=2)
        tk.Label(self.dm_cb.w, text="50%", width=5).grid(row=4, column=0+i)
        self.l_pwr50.grid(row=4, column=1+i, columnspan=2)
        tk.Label(self.dm_cb.w, text="70%", width=5).grid(row=5, column=0+i)
        self.l_pwr70.grid(row=5, column=1+i, columnspan=2)
        tk.Label(self.dm_cb.w, text="83%", width=5).grid(row=6, column=0+i)
        self.l_pwr83.grid(row=6, column=1+i, columnspan=2)
        tk.Label(self.dm_cb.w, text="87%", width=5).grid(row=7, column=0+i)
        self.l_pwr87.grid(row=7, column=1+i, columnspan=2)
        tk.Label(self.dm_cb.w, text="95%", width=5).grid(row=8, column=0+i)
        self.l_pwr95.grid(row=8, column=1+i, columnspan=2)
        self.l_gps.grid(row= 9, column=0+i, columnspan=3)
        self.c_log.grid(row=10, column=0+i, columnspan=3)

    def update_params(self):
        f = None
        while self.running:
            sleep(self.mpm.pa_cs/self.mpm.pa_sr*1.05)
            cal = self.mpm.get_cal()
            sig = self.mpm.get_sig()
            loc = self.dm_cb.gpm.get_gps()
            sam = self.mpm.get_samples()
            tmp = self.mpm.get_sig_at([15, 50, 70, 83, 87, 95])
            try:
                self.sv_cal.set("%5.1f" % cal)
                if sig: self.sv_pwr.set("%7.1f" % sig)
                if tmp:
                    self.sv_pwr15.set("%7.1f dBm" % tmp[0])
                    self.sv_pwr50.set("%7.1f dBm" % tmp[1])
                    self.sv_pwr70.set("%7.1f dBm" % tmp[2])
                    self.sv_pwr83.set("%7.1f dBm" % tmp[3])
                    self.sv_pwr87.set("%7.1f dBm" % tmp[4])
                    self.sv_pwr95.set("%7.1f dBm" % tmp[5])
                if loc[0] or loc[1]: self.sv_gps.set("%9.4f, %9.4f" % loc)
                # check the Logging setting and react appropriately
                if self.iv_log.get() and not self.logging:
                    print("%s: Requested logging" % self.name)
                    if loc[0] or self.dm_cb.gpm.log_loc_override:
                        try:
                            fn = self.new_fn()
                            f = open(fn, "a")
                            self.logging = True
                        except IOError:
                            print("%s: Couldn't open logfile!" % self.name)
                            print("%s: used %s" % (self.name, fn))
                            print("Error info:")
                            print(sys.exc_info(), end="\n\n")
                            self.c_log.deselect()
                            self.c_log.flash()
                        except:
                            print("%s: Couldn't create logfile name" \
                                  % self.name)
                            print("Error info:")
                            print(sys.exc_info(), end="\n\n")
                    else:
                        print("Logging cannot be enabled due to GPS failure.")
                        self.c_log.deselect()
                elif self.logging and not self.iv_log.get():
                    print("%s: Requested to close logging" % self.name)
                    try: f.close()
                    except: pass
                    f = None
                    self.logging = False
                if self.logging:
                    pctl = [0, 5, 10, 15, 25, 40, 50, 60, 65, 70, 75, 80, 83,
                            85, 87, 90, 93, 95, 98, 99]
                    tmp = self.mpm.get_sig_at(pctl)
                    if tmp and (loc[0] or self.dm_cb.gpm.log_loc_override):
                        tmp = ', '.join(tuple(["%4.1f:%6.1f" % (x, y) \
                                              for x, y in zip(pctl, tmp)]))
                        ts = strftime("%Y-%m-%d.%H:%M:%S.%z")
                        try:
                            f.write("%s, %9.6f,%10.6f, %7.2f, %d, %s\n" % \
                                    (strftime("%Y-%m-%d.%H:%M:%S.%z"),
                                     loc[0], loc[1], sig, sam, tmp))
                        except IOError:
                            print("Couldn't write to logfile!")
                            try: f.close()
                            except: pass
                            f = None
                            self.logging = False # next loop will try to reopen
                            self.c_log.flash
            except RuntimeError:
                sleep(0.05)
                continue # our main loop has probably been terminated
        if f:
            print("%s: Closing file due to program exit." % self.name)
            f.close()
        try: self.mpm.pa.terminate()
        except: pass

    def cal_up(self):
        self.cal = self.mpm.cal_up()
        self.sv_cal.set("%5.1f" % self.mpm.get_cal())

    def cal_dn(self):
        self.cal = self.mpm.cal_dn()
        self.sv_cal.set("%5.1f" % self.mpm.get_cal())

    def new_fn(self):
        """Compute a new filename for logging"""
        if not hasattr(self, "last_fn"): self.last_fn = ""
        return "siglog_" + chr(65+max(0, min(26, self.instance))) + "_" + \
               strftime("%Y-%m-%d_%H.%M.%S") + ".log"

    def start_audio(self):
        """Load a new MultiParametersManager and try to stop the old one"""
        if hasattr(self, "mpm"):
            try:
                if isinstance(self.mpm, MultiParametersManager):
                    self.mpm.stop_audio()
            except:
                print("Failing to stop existing audio on shim instance %d" % \
                      self.instance)
            del self.mpm
        self.mpm = MultiParametersManager(adev=self.adev, ach=self.channel)
        if self.init_cal != None: self.mpm.cal = self.init_cal
        elif self.channel != -1: self.mpm.cal -= 26 # minimum gain of Aux VFO
        self.mpm.start_audio()

    def stop(self):
        """Cleanly exit the main loop."""
        self.running = False
        self.mpm.stop_audio()


class MultiDisplayManager(object):
    def __init__(self, cfg="smeter-multi.ini"):
        print("Starting Signal Logger v%d.%02x" % (VERSION >> 8, VERSION % 256))
        self.running = True
        self.logging = False
        self.read_config(cfg)
        self.make_window()
        self.gpm = GlobalParametersManager(self._comport[0],
                                           serial_url=self._comport[1])
        self.gpm.log_loc_override = self._llo
        self.gpm.start_gps()
        self.instances = len(self.shims)
        # please do not assume I made the rest of this method before 2AM
        # initialize each RFDataShim
        [shim.add_into_window() for shim in self.shims]
        # start main-loop threads inside shims
        for i in range(self.instances):
            self.shims[i].start_audio()
            self.shims[i].thread = threading.Thread(\
                target=self.shims[i].update_params,
                daemon=True)
            self.shims[i].thread.start()
        self.w.mainloop()
        self.stop()
        print("Exiting Signal Logger v%d.%02x" % (VERSION >> 8, VERSION % 256))

    def read_config(self, cfg_fn):
        """Read config, store serial config, and make list of shims"""
        cp = configparser.RawConfigParser()
        cp.read_dict(DEFAULT_CONFIG)
        if not cp.read(cfg_fn):
            print("Failed to load config!")
        self._llo = cp.getboolean("Global", "log_without_gps", fallback=False)
        # Set up comport parameters
        comport = cp.get("Global", "gps_port", fallback="COM1")
        combaud = cp.getint("Global", "gps_baud", fallback=9600)
        combits = cp.getint("Global", "gps_bits", fallback=8)
        compari = cp.get("Global", "gps_parity", fallback="N")
        comstop = cp.get("Global", "gps_stopbits", fallback="1")
        comflow = cp.get("Global", "gps_flowcontrol", fallback="None")
        comnete = cp.getboolean("Global", "gps_tcp", fallback=False)
        comurl  = cp.get("Global", "gps_url", fallback="")
        # comport word size
        if combits == 8: combits = serial.EIGHTBITS
        elif combits == 7: combits = serial.SEVENBITS
        elif combits == 5: combits = serial.FIVEBITS # can we parse this?
        elif combits == 6: combits = serial.SIXBITS #  can we parse this?
        else: combits = serial.EIGHTBITS
        # comport parity
        if compari == "N": compari = serial.PARITY_NONE
        elif compari == "E": compari = serial.PARITY_EVEN
        elif compari == "O": compari = serial.PARITY_ODD
        elif compari == "M": compari = serial.PARITY_MARK
        elif compari == "S": compari = serial.PARITY_SPACE
        else: compari = serial.PARITY_NONE
        # what is serial.PARITY_NAMES ?
        # comport stopbits
        if comstop == "1": comstop = serial.STOPBITS_ONE
        elif comstop == "1.5": comstop = serial.STOPBITS_ONE_POINT_FIVE
        elif comstop == "2": comstop = serial.STOPBITS_TWO
        else: comstop = serial.STOPBITS_ONE
        comflow = map(lambda q:q.strip().casefold(), comflow.split(','))
        self._comport = ({"port":comport,
                          "baudrate":combaud,
                          "bytesize":combits,
                          "parity":compari,
                          "stopbits":comstop,
                          "xonxoff":("xonxoff" in comflow or \
                                     "xon/xoff" in comflow),
                          "rtscts":("rtscts" in comflow or \
                                    "rts/cts" in comflow),
                          "dsrdtr":("dsrdtr" in comflow or \
                                    "dsr/dtr" in comflow)},
                         (comurl if comnete else None))
        # identify the RFDataShims to load
        sections = cp.sections()
        try: sections.remove("Global")
        except ValueError: pass
        if not (len(sections) == 1 and sections[0].startswith("__")):
            sections.pop(0)
        self.shims = []
        for section in sections:
            try:
                self.shims.append(RFDataShim\
                                  (self,
                                   len(self.shims),
                                   (cp.get(section, "source",
                                           fallback=AUDIO_DEVICE),
                                    cp.getint(section, "channel",
                                              fallback=0)-1),
                                   name=section,
                                   init_cal=cp.getfloat(section, "cal",
                                                        fallback=None)))
            except:
                print("Failed to load config for %s" % section)
                print(sys.exc_info())
        

    def make_window(self):
        self.w = tk.Tk()
        self.w.title("Signal Logger v%d.%02x" % \
                     (VERSION >> 8, VERSION % 256))

    def stop(self):
        self.gpm.stop_gps()
        for i in range(self.instances):
            self.shims[i].stop()
        self.running = False
        timeout_counter = 1
        while any(map(lambda q:q.thread.is_alive(), self.shims)) and \
              timeout_counter < 20:
            if timeout_counter % 10 == 0:
                print("Wait for threads to close, have %d..." % \
                      threading.active_count())
            sleep(0.5)
            timeout_counter += 1
        try: self.w.destroy()
        except: pass


def main():
    global d
    d = MultiDisplayManager()

if __name__ == "__main__": main()
