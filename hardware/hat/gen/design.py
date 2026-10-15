"""Hasler HAT rev A - the netlist, once.

Both the schematic (make_sch.py) and the board (make_pcb.py) are generated from
this file, so they cannot disagree. Decisions and their reasons: docs/hat/DESIGN.md.

Python 3.9, no dependencies: make_pcb.py runs under KiCad's own interpreter.
"""

REV = "A"
TITLE = "Hasler HAT"
COMPANY = "Delgado & Murphy"

# Footprints
R0603 = "Resistor_SMD:R_0603_1608Metric"
C0603 = "Capacitor_SMD:C_0603_1608Metric"
C0402 = "Capacitor_SMD:C_0402_1005Metric"
C0805 = "Capacitor_SMD:C_0805_2012Metric"
C1206 = "Capacitor_SMD:C_1206_3216Metric"
RARR = "Resistor_SMD:R_Array_Convex_4x0603"
LED0603 = "LED_SMD:LED_0603_1608Metric"
SMA = "Diode_SMD:D_SMA"
SOT23_3 = "Package_TO_SOT_SMD:SOT-23-3"
SOT23_6 = "Package_TO_SOT_SMD:SOT-23-6"
TP = "TestPoint:TestPoint_Pad_D1.0mm"
SJ2B = "Jumper:SolderJumper-2_P1.3mm_Bridged_RoundedPad1.0x1.5mm"
SJ2O = "Jumper:SolderJumper-2_P1.3mm_Open_RoundedPad1.0x1.5mm"
SJ3 = "Jumper:SolderJumper-3_P1.3mm_Bridged12_RoundedPad1.0x1.5mm"

# LCSC part numbers (JLCPCB).
LCSC = {
    "10k": "C25804", "1k": "C21190", "100R": "C22775", "220R": "C22962",
    "100k": "C25803", "120k": "C25808", "1M": "C22935",
    "4x10k": "C29718", "4x1k": "C20197",
    "100nF/16V": "C1525", "1uF/X7R": "C28323", "1uF": "C15849", "10uF": "C15850", "10uF/50V": "C13585",
    "220uF/35V": "C970697", "2M": "C22976", "12k": "C22790", "2.2nF/50V": "C1604", "22nF": "C1532",
}

COMPONENTS = []


def part(ref, lib_id, value, footprint, pins, lcsc=None, block="", dnp=False,
         mpn=None, desc=None):
    """pins: {pin number: net name}. Pins left out are not connected (flagged)."""
    COMPONENTS.append(dict(ref=ref, lib_id=lib_id, value=value, footprint=footprint,
                           pins=dict(pins), lcsc=lcsc, block=block, dnp=dnp,
                           mpn=mpn or value, desc=desc or ""))


def R(ref, value, a, b, block, dnp=False):
    part(ref, "Device:R", value.replace("R", "") + ("Ω" if value.endswith("R") else ""),
         R0603, {"1": a, "2": b}, LCSC[value], block, dnp)


def C(ref, value, a, b, block, fp=C0603, dnp=False):
    part(ref, "Device:C", value.split("/")[0], fp, {"1": a, "2": b}, LCSC[value], block, dnp,
         desc=value)


def TPt(ref, net, label):
    part(ref, "Connector:TestPoint", label, TP, {"1": net}, None, "test")


# ------------------------------------------------------------------ power in
B = "power"
part("J3", "Connector:USB_C_Receptacle_USB2.0_16P", "USB-C PD in",
     "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12",
     {"A1": "GND", "A12": "GND", "B1": "GND", "B12": "GND", "S1": "GND",
      "A4": "VBUS", "A9": "VBUS", "B4": "VBUS", "B9": "VBUS",
      "A5": "CC1", "B5": "CC2", "A6": "USB_DP", "B6": "USB_DP",
      "A7": "USB_DM", "B7": "USB_DM"},
     "C165948", B, mpn="TYPE-C-31-M-12")
part("U3", "hasler:CH224A", "CH224A",
     "Package_SO:SSOP-10-1EP_3.9x4.9mm_P1mm_EP2.1x3.3mm",
     {"11": "GND", "1": "VBUS", "2": "PD_SCL", "3": "PD_SDA", "4": "USB_DP",
      "5": "USB_DM", "6": "CC2", "7": "CC1", "8": "VBUS", "9": "PD_CFG1", "10": "PD_PG"},
     "C42459160", B, desc="USB PD sink, 120k on CFG1 = 20 V")
R("R1", "120k", "PD_CFG1", "GND", B)                    # 20 V request (210k would be 28 V!)
C("C1", "1uF/X7R", "VBUS", "GND", B, fp=C0805)            # VHV/VBUS pins, right under U3: X7R keeps its 1 uF at 20 V
C("C2", "1uF", "VBUS", "GND", B)                        # connector side, USB PD: <= 10 uF
part("D1", "Device:D_Zener", "SMAJ22A", SMA, {"1": "VBUS", "2": "GND"},
     "C283896", B, desc="TVS 22 V standoff, unidirectional")
R("R2", "10k", "VBUS", "LED_PD", B)
part("D2", "Device:LED", "PD", LED0603, {"1": "PD_PG", "2": "LED_PD"}, "C2287", B,
     mpn="KT-0603Y", desc="yellow: PD contract made")
part("JP9", "Jumper:SolderJumper_2_Open", "PD_SCL", SJ2O, {"1": "PD_SCL", "2": "SCL"}, None, B)
part("JP10", "Jumper:SolderJumper_2_Open", "PD_SDA", SJ2O, {"1": "PD_SDA", "2": "SDA"}, None, B)

# ------------------------------------------------ eFuse: over-voltage, inrush, current limit
# TI TPS259474A (auto-retry, circuit breaker, PG): adjustable UVLO and OVLO (1.2 us),
# dV/dt-controlled start-up whatever the load capacitance, true reverse-current blocking
# (the Amp4's barrel jack can no longer back-feed the USB-C), thermal shutdown. IN 2.7-23 V,
# 28 V absolute. Thresholds: VUVLO = VOVLO = VPGTH = 1.20 V rising / 1.09 V falling.
B = "efuse"
part("U4", "hasler:TPS259474", "TPS259474ARPWR", "hasler:TI_RPW0010A_VQFN-HR-10_2x2mm_P0.45mm",
     {"1": "EF_EN", "2": "EF_OV", "3": "EF_PG", "4": "EF_PGTH", "5": "VBUS", "6": "VOUT",
      "7": "EF_DVDT", "8": "GND", "9": "EF_ILM", "10": "EF_TMR"},
     "C3662807", B, desc="eFuse 2.7-23 V 5.5 A, OVLO, circuit breaker, PG, auto-retry")
C("C5", "1uF", "VBUS", "GND", B)                         # at IN (VBUS total ~3 uF, PD allows 10)
# EN/UVLO. JP11 open (as shipped): EN is pulled up by R3 alone (TI allows >= 350k to IN), so U4
# turns on with the charger's first 5 V, before the PD contract, and the 5 -> 20 V step happens
# with U4 already fully on: no linear start-up from 0 to 20 V into a load that starts drawing
# part-way up the ramp (the Amp4's buck and the Pi, which can overheat U4: see DESIGN.md).
# JP11 bridged: UVLO 1.2 V x 1132/132 = 10.3 V on (9.9-10.7 V worst case), 1.09 V x 8.58 =
# 9.35 V off: no 5 V / 9 V to the amp, at the price of that linear start-up.
R("R3", "1M", "VBUS", "EF_EN", B)
R("R4", "120k", "EF_EN", "EF_ENB", B)
R("R16", "12k", "EF_ENB", "EF_ENJ", B)
part("JP11", "Jumper:SolderJumper_2_Open", "UVLO_10V", SJ2O, {"1": "EF_ENJ", "2": "GND"}, None, B)
R("R5", "2M", "VBUS", "EF_OV", B)                       # OVLO: 1.2 V x 2112/112 = 22.6 V off (21.7-23.7 V
R("R6", "100k", "EF_OV", "EF_OVB", B)                   #   worst case incl. 1 % and pin leakage; PD 20 V
R("R7", "12k", "EF_OVB", "GND", B)                      #   +5 % = 21 V), 1.09 V x 18.86 = 20.55 V on again
R("R8", "1M", "VOUT", "EF_PGTH", B)                     # PG once VOUT > 11.2 V
R("R9", "120k", "EF_PGTH", "GND", B)
R("R15", "100k", "+3V3", "EF_PG", B)                    # PG is open drain, <= 5 V: to the Pi's 3V3
part("R14", "Device:R", "1k", "Resistor_SMD:R_0402_1005Metric", {"1": "EF_ILM", "2": "GND"}, "C11702", B)
                                                        # ILIM = 3334 / 1k = 3.3 A (65 W charger: 3.25 A)
C("C4", "22nF", "EF_TMR", "GND", B, fp=C0402)                   # overcurrent blanking 1.51 V x 22 nF / 1.8 uA = 18 ms
C("C3", "2.2nF/50V", "EF_DVDT", "GND", B)               # dV/dt ~1.0 V/ms at 20 V (0.3-1.75 worst case over IdVdt)
C("C17", "1uF", "VOUT", "GND", B)                        # ceramic right at OUT (TI 8.3.1)

# ------------------------------------------------------------- out to amp
B = "out"
C("C7", "220uF/35V", "VOUT", "GND", B, fp="Capacitor_SMD:CP_Elec_8x10", dnp=True)   # not fitted: the
# eFuse charges whatever sits on VOUT at its dV/dt, and the Amp4 has its own input capacitor;
# 220 uF more only doubles the start-up heating in U4. The pads stay for an amp that needs it.
COMPONENTS[-1]["lib_id"] = "Device:C_Polarized"
C("C8", "10uF/50V", "VOUT", "GND", B, fp=C1206)
part("D5", "Device:D_Zener", "SMAJ22A", SMA, {"1": "VOUT", "2": "GND"}, "C283896", B)
R("R10", "10k", "VOUT", "LED_OUT", B)
part("D6", "Device:LED", "OUT", LED0603, {"1": "GND", "2": "LED_OUT"}, "C965804", B,
     mpn="XL-1608UGC-04", desc="green: 20 V to the amp")
part("J6", "Connector:Screw_Terminal_01x02", "To Amp4 DC in",
     "hasler:TerminalBlock_Kefa_KF350-3.5-2P_1x02_P3.50mm_Horizontal",
     {"1": "VOUT", "2": "GND"}, "C474892", B, mpn="KF350-3.5-2P")

# ------------------------------------------------------- 3V3A (cabinet rail)
B = "3v3a"
part("U6", "Regulator_Linear:XC6206PxxxMR", "XC6206P332MR", SOT23_3,
     {"3": "+5V", "1": "GND", "2": "LDO_OUT"}, "C5446", B, mpn="XC6206P332MR-G")
C("C9", "1uF", "+5V", "GND", B)
C("C10", "1uF", "LDO_OUT", "GND", B)
part("JP1", "Jumper:SolderJumper_3_Bridged12", "3V3A_SRC", SJ3,
     {"1": "LDO_OUT", "2": "3V3A", "3": "+3V3"}, None, B,
     desc="1-2 LDO (default), 2-3 Pi 3V3")
C("C11", "10uF", "3V3A", "GND", B, fp=C0805)
C("C12", "100nF/16V", "3V3A", "GND", B, fp=C0402)
R("R11", "1k", "3V3A", "LED_3V3", B)
part("D7", "Device:LED", "3V3", LED0603, {"1": "GND", "2": "LED_3V3"}, "C2286", B,
     mpn="KT-0603R", desc="red: 3V3A")
C("C13", "10uF", "+3V3", "GND", B, fp=C0805)

# ------------------------------------------------------------ Pi socket
B = "pi"
PI = {1: "+3V3", 2: "+5V", 3: "SDA", 4: "+5V", 5: "SCL", 6: "GND",
      8: "TXD", 9: "GND", 10: "RXD", 11: "GPIO17", 13: "GPIO27", 14: "GND",
      15: "GPIO22", 16: "GPIO23", 17: "+3V3", 18: "GPIO24", 20: "GND", 22: "GPIO25",
      25: "GND", 29: "GPIO5", 30: "GND", 31: "GPIO6", 32: "GPIO12", 33: "GPIO13",
      34: "GND", 36: "GPIO16", 37: "GPIO26", 39: "GND"}
part("J1", "Connector_Generic:Conn_02x20_Odd_Even", "Amp4 top header",
     "Connector_PinSocket_2.54mm:PinSocket_2x20_P2.54mm_Vertical",
     {str(k): v for k, v in PI.items()}, "C5124634", B, mpn="2.54-2*20P female 8.5 mm",
     desc="on the bottom side, onto the Amp4 Pro's offset top header")

# ------------------------------------------------------------- MCP23017
B = "mcp"
DIG = [  # (J2 pin, signal, MCP pin number, MCP name)
    (1, "K1", "1", "GPB0"), (3, "K2", "2", "GPB1"), (4, "K3", "3", "GPB2"),
    (5, "K4", "4", "GPB3"), (6, "K5", "5", "GPB4"), (7, "K6", "6", "GPB5"),
    (8, "PU", "7", "GPB6"), (14, "TSW", "21", "GPA0"), (17, "X1", "22", "GPA1"),
    (18, "X2", "23", "GPA2"), (19, "X3", "24", "GPA3"), (20, "X4", "25", "GPA4"),
    (21, "X5", "26", "GPA5"), (22, "X6", "27", "GPA6"),
]
ANA = [(9, "A0", "4"), (10, "A1", "5"), (23, "A2", "6"), (25, "A3", "7")]  # ADS AINx pin
LAMP = [(13, "LR", "GPIO17", "220R"), (15, "LG", "GPIO22", "100R"), (16, "LB", "GPIO27", "100R")]

mcp = {"9": "+3V3", "10": "GND", "12": "SCL", "13": "SDA", "15": "MCP_A0", "16": "MCP_A1",
       "17": "MCP_A2", "18": "MCP_RST", "20": "MCP_INTA"}
for _, sig, num, _ in DIG:
    mcp[num] = "M_" + sig
part("U1", "Interface_Expansion:MCP23017_SO", "MCP23017", "Package_SO:SSOP-28_5.3x10.2mm_P0.65mm",
     mcp, "C506653", B, mpn="MCP23017-E/SS")
C("C14", "100nF/16V", "+3V3", "GND", B, fp=C0402)
for i, a in enumerate(("MCP_A0", "MCP_A1", "MCP_A2")):
    part("JP%d" % (2 + i), "Jumper:SolderJumper_3_Bridged12", "A%d" % i, SJ3,
         {"1": "GND", "2": a, "3": "+3V3"}, None, B, desc="1-2 GND (0x20)")
R("R12", "10k", "+3V3", "MCP_RST", B)
part("JP7", "Jumper:SolderJumper_2_Open", "RST_GPIO25", SJ2O, {"1": "MCP_RST", "2": "GPIO25"}, None, B)
part("JP5", "Jumper:SolderJumper_2_Bridged", "INT_GPIO23", SJ2B, {"1": "MCP_INTA", "2": "GPIO23"}, None, B)

# ------------------------------------------------------------- ADS1115
B = "ads"
ads = {"8": "3V3A", "3": "GND", "10": "SCL", "9": "SDA", "1": "ADS_ADDR", "2": "ADS_RDY"}
for _, sig, num in ANA:
    ads[num] = "M_" + sig
part("U2", "Analog_ADC:ADS1115IDGS", "ADS1115", "Package_SO:VSSOP-10_3x3mm_P0.5mm",
     ads, "C37593", B, mpn="ADS1115IDGSR")
C("C15", "100nF/16V", "3V3A", "GND", B, fp=C0402)
C("C16", "1uF", "3V3A", "GND", B)
part("JP8", "Jumper:SolderJumper_2_Bridged", "ADDR_GND", SJ2B, {"1": "ADS_ADDR", "2": "GND"}, None, B,
     desc="ADDR to GND = 0x48")
R("R13", "10k", "+3V3", "ADS_RDY", B)                   # Pi-side pull-up: no back-feed into an unpowered Pi
part("JP6", "Jumper:SolderJumper_2_Bridged", "RDY_GPIO24", SJ2B, {"1": "ADS_RDY", "2": "GPIO24"}, None, B)

# ------------------------------------------------- cabinet line conditioning
B = "lines"
# Order along J2 so the arrays sit in a row behind the connector.
ESD_ORDER = ["K1", "K2", "K3", "K4", "K5", "K6", "PU", "A0", "A1", "LR", "TSW", "LG",
             "LB", "X1", "X2", "X3", "X4", "X5", "X6", "A2", "A3"]
ESD_PINS = ["1", "3", "4", "6"]  # IO1..IO4 of SRV05-4
for n in range(6):
    pins = {"5": "3V3A", "2": "GND"}
    for k, sig in enumerate(ESD_ORDER[4 * n:4 * n + 4]):
        pins[ESD_PINS[k]] = "J_" + sig
    part("U%d" % (7 + n), "Power_Protection:SRV05-4", "SRV05-4", SOT23_6, pins, "C13612", B,
         mpn="SRV05-4.TCT")
    C("C%d" % (38 + n), "100nF/16V", "3V3A", "GND", B, fp=C0402)   # at its VCC pin

# Lines in fours: one 1k series array per group (digital groups, then the analog one).
DIG_GROUPS = [["K1", "K2", "K3", "K4"], ["K5", "K6", "PU", "TSW"], ["X1", "X2", "X3", "X4"], ["X5", "X6"]]
ANA_GROUP = ["A0", "A1", "A2", "A3"]
SERIES = [s for g in DIG_GROUPS for s in g] + ANA_GROUP          # 18 lines, 1k each
for n, grp in enumerate(DIG_GROUPS + [ANA_GROUP]):                # RN1-4 digital, RN5 analog
    pins = {}
    for k, sig in enumerate(grp):
        pins[str(k + 1)] = "J_" + sig          # R_Pack04: Rk = pins k / 9-k
        pins[str(8 - k)] = "M_" + sig
    part("RN%d" % (1 + n), "Device:R_Pack04", "4x1k", RARR, pins, LCSC["4x1k"], B,
         mpn="4D03WGJ0102T5E")

# RN6-9: 10k pull-ups on the cabinet side of the 1k, beside the ESD arrays: the ribbon gets
# its bias right at the connector, and the MCP side of each line is just 1k + 100 nF + pin.
for n, grp in enumerate(DIG_GROUPS):
    pins = {}
    for k, sig in enumerate(grp):
        pins[str(k + 1)] = "+3V3"
        pins[str(8 - k)] = "J_" + sig
    part("RN%d" % (6 + n), "Device:R_Pack04", "4x10k", RARR, pins, LCSC["4x10k"], B,
         mpn="4D03WGJ0103T5E")

for k, sig in enumerate(SERIES):
    C("C%d" % (20 + k), "100nF/16V", "M_" + sig, "GND", B, fp=C0402)

for k, (_, sig, gpio, val) in enumerate(LAMP):
    R("R%d" % (20 + k), val, "J_" + sig, gpio, B)

# ------------------------------------------------------------------ J2
B = "j2"
J2 = {2: "GND", 11: "GND", 24: "GND", 12: "3V3A", 26: "3V3A"}
for pin, sig, _, _ in DIG:
    J2[pin] = "J_" + sig
for pin, sig, _ in ANA:
    J2[pin] = "J_" + sig
for pin, sig, _, _ in LAMP:
    J2[pin] = "J_" + sig
assert sorted(J2) == list(range(1, 27)), sorted(J2)
# What the cabinet wire on each J2 pin is (HM / Excelsior), for the silkscreen legend.
J2_LABELS = {1: "K1 / key1", 2: "GND", 3: "K2 / key2", 4: "K3 / key3", 5: "K4 / key4",
             6: "K5 / key5", 7: "K6 / key6", 8: "PU / Q", 9: "tone/treble", 10: "volume",
             11: "pots GND", 12: "3V3A pots,LED+", 13: "LED red", 14: "tone sw/key0",
             15: "LED green", 16: "LED blue", 17: "- / Tape", 18: "- / SP", 19: "- / spk a",
             20: "- / spk b", 21: "spare", 22: "spare", 23: "- / bass", 24: "GND",
             25: "spare A3", 26: "3V3A"}
part("J2", "Connector_Generic:Conn_02x13_Odd_Even", "Hasler J2",
     "Connector_IDC:IDC-Header_2x13_P2.54mm_Vertical",
     {str(k): v for k, v in J2.items()}, "C5156668", B, mpn="DC3-2.54-26PAS",
     desc="keyed shrouded box header, DIN 41651 numbering")

# ------------------------------------------------------- debug / expansion
B = "debug"
J4 = ["+5V", "+3V3", "GND", "GND", "SDA", "SCL", "TXD", "RXD", "GPIO5", "GPIO6",
      "GPIO12", "GPIO13", "GPIO16", "GPIO26", "3V3A", "GND"]
part("J4", "Connector_Generic:Conn_02x08_Odd_Even", "Debug / expansion",
     "Connector_PinHeader_2.54mm:PinHeader_2x08_P2.54mm_Vertical",
     {str(i + 1): n for i, n in enumerate(J4)}, "C2894969", B, mpn="PZ254-2-08-Z-8.5")
part("J5", "Connector_Generic:Conn_01x04", "Qwiic",
     "Connector_JST:JST_SH_SM04B-SRSS-TB_1x04-1MP_P1.00mm_Horizontal",
     {"1": "GND", "2": "+3V3", "3": "SDA", "4": "SCL"}, "C160404", B, mpn="SM04B-SRSS-TB")

for i, (net, label) in enumerate([
        ("VBUS", "VBUS"), ("VOUT", "20V"), ("+5V", "5V"), ("+3V3", "3V3"), ("3V3A", "3V3A"),
        ("GND", "GND"), ("GND", "GND"), ("SDA", "SDA"), ("SCL", "SCL"), ("MCP_INTA", "INT"),
        ("PD_PG", "PD"), ("EF_PG", "PG"), ("M_A0", "A0"), ("M_A1", "A1")]):
    TPt("TP%d" % (1 + i), net, label)

for i in (1, 2):
    part("H%d" % i, "Mechanical:MountingHole", "M2.5", "MountingHole:MountingHole_2.7mm_M2.5",
         {}, None, "mech")

# Nets that need a PWR_FLAG for ERC (driven from off-sheet: USB, the Pi, the amp).
PWR_FLAGS = ["VBUS", "GND", "+5V", "+3V3", "3V3A"]     # VOUT: driven by the eFuse

POWER_NETS = {"VBUS", "VOUT"}                  # up to 3 A: wide tracks
GND_NETS = {"GND"}                             # plane on In1, vias down from every pad
RAIL_NETS = {"+5V", "+3V3", "3V3A", "LDO_OUT"}


def nets():
    out = {}
    for c in COMPONENTS:
        for pin, net in c["pins"].items():
            out.setdefault(net, []).append((c["ref"], pin))
    return out


if __name__ == "__main__":
    ns = nets()
    for n in sorted(ns):
        print("%-10s %s" % (n, " ".join("%s.%s" % rp for rp in ns[n])))
    single = [n for n, v in ns.items() if len(v) < 2]
    print(len(COMPONENTS), "components,", len(ns), "nets; single-pin nets:", single)
