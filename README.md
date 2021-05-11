# Signal-Logger
Correlate RF signal reception with location to calibrate coverage maps using SDR.

*Disclaimer: I do not understand the specifics of the math behind this program; I just know it's reasonably accurate. This program requires a fairly specific receiver setup that (probably) only works on Windows, despite the program itself being (probably) cross-platform. The default calibration matches my receiver setup and won't match yours, so you'll need to find a way to calibrate the signal readout yourself in order to get useful results. Eventually I'll add known-good values for a naked RTL-SDR. *

## What exactly does this do?
This program reads audio output by [SDR#](https://airspy.com/download/) and converts it into absolute signal strength values. The large number is RMS, and the smaller numbers are 'signal strength n% of the time' (for example, the signal strength a received signal exceeds 70% of the time; useful for mobile situations). At the bottom is a position readout from a serial-connected GPS using NMEA 0183 protocol (Signal Logger parses the GPGGA message, and validates checksums). Logging can be enabled, which stores the date/time, RMS signal level, signal level at various thresholds (more than what's displayed, for interpolation later), and location into a comma-separated text file with one line per display refresh. 

## How is YOUR receiver set up?
I use an [RTL-SDR Blog V3 dongle](https://www.rtl-sdr.com/buy-rtl-sdr-dvb-t-dongles/) connected to a [RTL-SDR Blog Wideband LNA](https://www.rtl-sdr.com/new-products-in-our-store-wideband-lna-spare-metal-v3-enclosures/) and a ~ 200 MHz highpass filter to eliminate FM broadcast and local police/fire signals that could cause the SDR to go into front-end overload. I run a fairly low RTL gain (usually index 11, which I think is around 20dB gain); you usually don't care about receiving signal strengths below -120dBm since most radios need about -116dBm for intelligible speech, and you want to have as much headroom available as possible for strong in-band or out-of-band signals to prevent front-end overload. Turn off ALL the AGC options! This program works on the principle of the SDR being capable of truly fixed gain. 

I then tell SDRSharp to use [VB-Cable Hi-Fi](https://vb-audio.com/Cable/#DownloadASIOBridge) as the audio output, demodulator mode RAW (I/Q stereo output to soundcard), with Unity Gain enabled. At my settings (with gain somewhat lower than the optimum, external-noise-limited, value), I can resolve signals from about -115 dBm to -45 dBm. 

## How do I set up MY receivers?

### Audio Endpoints
Before you can start telling Signal Logger where to find audio, you have to install a virtual audio cable or few in order for the audio, from which signal levels are derived, to travel between programs. I recommend [VB-Cable](https://vb-audio.com/Cable/) and [VB-Cable Hi-Fi](https://vb-audio.com/Cable/#DownloadASIOBridge). In your Windows Control Panel, Sound settings, you'll probably want to set the sample formats to 48000 Hz, 16-bit for the inputs *and* outputs of your installed virtual audio cables; the input must always match the output regardless of what you set it to. 

### Global Config
Create a configuration file named `smeter-multi.ini` and add a section entitled `[Global]`. These are where global (non-per-receiver) settings are stored. Serial port settings exist in the Global section, and are addressed primarily by the parameters `gps_port` and `gps_baud`. So to use COM1 at 9600 bps, your Global section would look like:
```
[Global]
gps_port=COM1
gps_baud=9600
```
Other settings are available if you require non-standard port setup, such as flow control, parity, or different word sizes. 
- Word size is controlled by `gps_bits` and can be `7` or `8`.  
- Parity is controlled by `gps_parity` and can be `N` (None), `O` (Odd), or `E` (Even). 
- Number of stop bits is controlled by `gps_stopbits` and can be `1`, `1.5`, or `2`. On POSIX, `1.5` is not available due to a pySerial limitation. 
- Flow control is controlled by `gps_flow_control`, and is a comma-separated list of flow-control values (in case you need multiple, if pySerial even allows this). Allowed values are `Xon/Xoff`, `RTS/CTS`, and `DSR/DTR`. These are case-insensitive and the `/` can be omitted.
- I implement [pySerial URL handling](https://pythonhosted.org/pyserial/url_handlers.html) in this (set `gps_tcp` to True, and define the URL in `gps_url`); it's never been tested. I don't expect this to work and you shouldn't either. 
- The default port setup is `COM1`, 4800bps, 8N1. If `gps` settings aren't defined in the config file, those settings will be used. 

If you want to enable logging without GPS (such as for a fixed receiver), set `log_without_gps` to True. The default value is `False`. 

**GPS is not required** for this program to work. If you don't want to use GPS, just set it to an invalid port. 

### Receiver Config
Next, you want to define the receivers. Create a section entitled by whatever you want to name this receiver (every section aside from Global is assumed to be a receiver). Define two parameters, `source` and `channel`. 

- `source` is the name of the audio device; Signal Logger checks if any audio devices start with the string given in `source` (case-insensitive), and can be used as an output device (audio source). 
- `channel` is a number, and can be `0`, `1`, or `2`. If `0`, stereo I/Q is used. If `1`, the left channel as AM demodulated audio is used, and `2` is the right channel in AM. 
- If you know what the calibration value should be, set it in the `cal` property as a floating-point number. Otherwise, the default value is used (currently set at 46 until I come up with something better), and 26 is subtracted if the input is AM. Apparently +26dB above unity is the lowest volume supported by [Aux VFO](http://www.rtl-sdr.ru/page/novyj-plagin-3). 

If no receivers are defined, a default receiver will be created. It tries to open VB-Cable Hi-Fi (search string `hi-fi cable output`) as I/Q, with default calibration. 

## SDR# Setup
For the I/Q output (you only get one!), go into Audio, Input, and set that to a virtual audio cable's input. Under Radio, set the demodulator to RAW. Back in your Audio settings, ensure Unity Gain is checked. If you're using the Band Plan plugin (you probably have it), uncheck `Auto update radio settings` to avoid your VFO settings being reset when tuning. Set up Signal Logger to use the virtual audio cable's output, and you should start seeing numbers in the signal strength display. Adjust your calibration values (see the upcoming Calibration section), and you're good to go!

For additional detectors, you need the [Aux VFO plugin](http://www.rtl-sdr.ru/page/novyj-plagin-3). Install the plugin, and add as many instances as you need in Plugins.xml (see magicline.txt in Aux VFO's install folder). Open SDR#, and in your VFO settings (NOT the Aux VFO settings!): set demodulator mode to AM; uncheck Radio, `Squelch` and `Lock Carrier` if checked; uncheck Audio, `Unity Gain` and `Filter Audio`; and uncheck AGC, `Use AGC`. Set your bandwidth to something reasonable (wide enough to capture the entire target signal, narrow enough to not contaminate your readings with co-channel signals). If you're using the Band Plan plugin, uncheck `Auto update radio settings` to avoid your VFO settings being reset when tuning. Tune to a channel you want to monitor, and set one of your Aux VFOs to that channel using the `Set` button. Before enabling the Aux VFO, set the output device to an unused virtual audio cable and select either L (left channel) or R (right channel) output. You can have two Aux VFOs on the same output device as long as the channels are different. Set the volume to zero by dragging the volume slider all the way to the left, and move it up *one pixel* (tap the right arrow key once when the volume slider is selected and at zero). Set Signal Logger to use that virtual sound card, set the channel to match Aux VFO, enable the Aux VFO, and you should start seeing numbers in Signal Logger in the pane matching your configured receiver. Adjust your calibration values and you're good to go!

## Calibration
You'll need to calibrate the S-meter value against a known reference to obtain a reading in dBm. For a naked RTL-SDR at 25.4 dB gain, I think this is in the ballpark of -66.0. If you measure a signal externally at -82 dBm and Signal Logger says it's at -75 dBm, press `Cal-` 7 times to reduce the calibration value by 7 dB. The calibration settings are constant for each receiver of the same SDR dongle and demodulator type, and will be quite similar across different SDR dongles of the same model and gain/samplerate settings. Remember the insertion loss and/or gain of external components in your RF chain, such as filters and LNAs!

## What range of signals can I measure?
The I/Q output option gives about 70dB of range, while Aux VFO's AM output gives about 45dB of range. Aux VFO doesn't support unity gain (yet?) so the full amplitude range is not available to us. While there may be workarounds in SDR# itself, I have not explored them. 

## What's with all these "Multi-" references?
This program used to be able to read only one soundcard input, which was always I/Q with unity gain. I found myself in a situation where I wanted to monitor multiple signals within my RTL's passband simultaneously, as that would save me several signal surveying trips. So, I moved the display portion of the code to its own object, and gave each signal its own audio device. That's why many UI elements are duplicated. 

## I have the log output, what do I do with it?
I have a parser in the works that converts it into a KML file whose points are color-coded [Radio Mobile network style](http://radiomobile.pe1mew.nl/?The_program:General_functions:Coverage_plot_types). It'll be added to this repository when completed. 
