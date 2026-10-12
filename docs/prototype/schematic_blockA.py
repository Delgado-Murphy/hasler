import random, itertools, sys
W,H=1640,1100
RED,GND,SDA,SCL,AMB,EXP,TXT,MUT,DIM,BG="#E5484D","#9A948A","#46D17A","#5B8CFF","#FFB238","#CFC6B4","#EFE6D2","#8A8378","#5A534A","#14110F"
o=[]
def t(x,y,s,c=TXT,size=None,bold=False,anchor=None,ls=None):
    a=f' font-size="{size}"' if size else ''
    a+=' font-weight="700"' if bold else ''
    a+=f' text-anchor="{anchor}"' if anchor else ''
    a+=f' letter-spacing="{ls}"' if ls else ''
    s=s.replace("&","&amp;").replace("<","&lt;")
    o.append(f'<text x="{x}" y="{y}"{a} fill="{c}">{s}</text>')
def p(d,c,w=2.5,dash=None):
    a=f' stroke-dasharray="{dash}"' if dash else ''
    o.append(f'<path d="{d}" fill="none" stroke="{c}" stroke-width="{w}" stroke-linecap="round" stroke-linejoin="round"{a}/>')
def pin(x,y,c): o.append(f'<rect x="{x-5}" y="{y-5}" width="10" height="10" rx="1" fill="{BG}" stroke="{c}" stroke-width="1.5"/>')
def dot(x,y,c): o.append(f'<circle cx="{x}" cy="{y}" r="3.5" fill="{c}"/>')
def gnd(x,y,c=GND): p(f"M{x-12} {y} H{x+12} M{x-8} {y+6} H{x+8} M{x-4} {y+12} H{x+4}",c)
def nc(x,y): p(f"M{x-6} {y-6} l12 12 M{x+6} {y-6} l-12 12",RED)
def flag(x,y,label,c,right=True,w=None):
    w=w or 8+7*len(label)+12
    if right:  # points right, body starts at x
        o.append(f'<path d="M{x} {y-10} H{x+w-10} L{x+w} {y} L{x+w-10} {y+10} H{x} Z" fill="{BG}" stroke="{c}" stroke-width="1.5" stroke-linejoin="round"/>')
        t(x+8,y+4,label,c,bold=True)
    else:      # points left, body ends at x
        o.append(f'<path d="M{x} {y-10} H{x-w+10} L{x-w} {y} L{x-w+10} {y+10} H{x} Z" fill="{BG}" stroke="{c}" stroke-width="1.5" stroke-linejoin="round"/>')
        t(x-8,y+4,label,c,bold=True,anchor="end")
    return w
def res(x0,y,c):  # zigzag 48 wide
    d=f"M{x0} {y} l4 0"+"".join(f" l4 {-8 if i%2==0 else 8} l4 {8 if i%2==0 else -8}" for i in range(5))+" l4 0"
    p(d,c,2)

# ---------- column headers
t(36,24,"P · PI HEADER (AMP4 PRO PASS-THROUGH)",MUT,ls=1.2)
t(600,24,"U1 · MCP23017 BREAKOUT · LONG EDGE IN A FEMALE 1×20",MUT,ls=1.2)
t(1340,24,"J2 · HASLER CONNECTOR · 2×13",MUT,ls=1.2)

# ---------- rails
RAIL3=70; RAILG=1060
p(f"M300 {RAIL3} H560",RED,2.5); t(300,RAIL3-10,"+3V3 RAIL",RED,bold=True,ls=1)

t(568,RAIL3-10,"→ also J2·11, J2·25, U2 VDD",MUT)
t(36,616,"⏚ is one net: P9, U1, J2·1 / 12 / 26, U2 GND + ADDR",MUT)

# ---------- P header (odd row)
PX=200; py=lambda i: 170+i*52
Pp=[("1","3V3",RED),("3","GPIO2",SDA),("5","GPIO3",SCL),("7","—",DIM),("9","GND",GND),("11","GPIO17",RED),("13","GPIO27",SDA),("15","GPIO22",SCL)]
o.append(f'<rect x="36" y="{py(0)-26}" width="{PX-30}" height="{py(7)-py(0)+52}" rx="6" fill="{BG}" stroke="#3A352F" stroke-width="1.5"/>')
for i,(n,name,c) in enumerate(Pp):
    y=py(i); pin(PX,y,c if name!="—" else DIM)
    t(52,y+4,n,MUT,bold=True); t(84,y+4,name,TXT if name!="—" else MUT,size=12,bold=True)
t(36,py(7)+46,"odd row only · 7 Dupont jumpers · P pin 4 pulled",MUT)
# 3V3 to rail
p(f"M{PX+5} {py(0)} H300 V{RAIL3}",RED); dot(300,RAIL3,RED)
# caps at the entry
for k,(cx,name,val,pol) in enumerate([(350,"C1","220 µF","+"),(440,"C2","100 nF","")]):
    p(f"M{cx} {RAIL3} V{RAIL3+30}",RED,2); dot(cx,RAIL3,RED)
    if pol: p(f"M{cx-14} {RAIL3+30} H{cx+14}",TXT); p(f"M{cx-14} {RAIL3+40} Q{cx} {RAIL3+33} {cx+14} {RAIL3+40}",TXT); t(cx-22,RAIL3+32,"+",RED,bold=True,anchor="end")
    else: p(f"M{cx-14} {RAIL3+30} H{cx+14} M{cx-14} {RAIL3+38} H{cx+14}",TXT)
    p(f"M{cx} {RAIL3+38} V{RAIL3+50}",GND,2); gnd(cx,RAIL3+50)
    t(cx+20,RAIL3+28,name,TXT,bold=True); t(cx+20,RAIL3+42,val,MUT)
t(330,RAIL3+70,"at the rail entry · C1 stripe to GND",MUT)
# SDA / SCL
UL=600  # U1 left x
ULy={"VCC":200,"GND":240,"SCL":330,"SDA":290,"RST":400,"ITA":440,"ITB":480}
p(f"M{PX+5} {py(1)} H440 V{ULy['SDA']} H{UL-5}",SDA); t(214,py(1)-8,"SDA",SDA,bold=True)
p(f"M{PX+5} {py(2)} H420 V{ULy['SCL']} H{UL-5}",SCL); t(214,py(2)-8,"SCL",SCL,bold=True)
t(460,ULy['SDA']-8,"flying wire → 25·8",MUT); t(460,ULy['SCL']+18,"flying wire → 24·9",MUT)
# pin 7 empty
p(f"M{PX+5} {py(3)} H226",DIM,2,"4 4"); nc(236,py(3)); t(252,py(3)+4,"empty (amp mute)",RED)
# GND
p(f"M{PX+5} {py(4)} H240 V{py(4)+6}",GND); gnd(240,py(4)+6)
# lamp
for i,(r,val,lab,c,jp) in enumerate([("R1","220 Ω","LAMP_R",RED,14),("R2","100 Ω","LAMP_G",SDA,15),("R3","100 Ω","LAMP_B",SCL,16)]):
    y=py(5+i); p(f"M{PX+5} {y} H250",c,2); res(250,y,c); p(f"M298 {y} H330",c,2)
    t(274,y-14,f"{r} {val}",c,bold=True,anchor="middle")
    flag(330,y,f"{lab} → J2·{jp}",c)
t(36,py(7)+64,"lamp: common anode on J2·11 (+3V3) · GPIO low = lit",MUT)

# ---------- U1
UR=900; uy=lambda i: 150+i*42
edge=["PA7","PA6","PA5","PA4","PA3","PA2","PA1","PA0","GND","VCC","PB0","PB1","PB2","PB3","PB4","PB5","PB6","PB7","GND","VCC"]
o.append(f'<rect x="{UL}" y="110" width="{UR-UL}" height="{uy(19)-110+40}" rx="6" fill="{BG}" stroke="{AMB}" stroke-width="1.5"/>')
for name,y in ULy.items():
    c={"VCC":RED,"GND":GND,"SCL":SCL,"SDA":SDA}.get(name,DIM)
    pin(UL,y,c); t(UL+14,y+4,name,TXT if c!=DIM else MUT,size=12,bold=True)
p(f"M560 {RAIL3} V{ULy['VCC']} H{UL-5}",RED,2)
p(f"M{UL-5} {ULy['GND']} H560 V{ULy['GND']+8}",GND,2); gnd(560,ULy['GND']+8)
p(f"M{UL-5} {ULy['RST']} H540",DIM,1.5,"4 4"); t(530,ULy['RST']+4,"10 k pull-up on the board",MUT,anchor="end")
for n in ("ITA","ITB"): p(f"M{UL-5} {ULy[n]} H570",DIM,2,"4 4"); nc(560,ULy[n]); t(546,ULy[n]+4,"n.c.",MUT,anchor="end")
t(UL+24,620,"U1",TXT,size=22,bold=True)
for i,s in enumerate(["MCP23017 · green breakout","I²C 0x20 · A0–A2 → GND on the board","4.7 k SDA/SCL pull-ups on the board","~100 k input pull-ups in the chip","","right: the long edge in board order","PA7 … PA0, GND, VCC, PB0 … PB7, GND, VCC"]):
    t(UL+24,644+i*18,s,MUT)
# right pins
key={"PA4":("K1",2),"PA5":("K2",3),"PA3":("K3",4),"PA6":("K4",5),"PA2":("K5",6),"PA0":("K6",7),"PA1":("PU",8),"PB0":("TONE",13),
     "PB4":("EXP1",17),"PB1":("EXP2",18),"PB5":("EXP3",19),"PB2":("EXP4",20),"PB6":("EXP5",21),"PB3":("EXP6",22)}
JX=1340; jy=lambda n: 110+(n-1)*36 + (14 if n>16 else 0)
wires=[]
for i,name in enumerate(edge):
    y=uy(i)
    if name in key: c=AMB if key[name][1]<=16 else EXP
    else: c={"GND":GND,"VCC":RED}.get(name,DIM)
    pin(UR,y,c); t(UR-14,y+4,name,TXT if c!=DIM else MUT,size=12,bold=True,anchor="end")
    if name in key: wires.append((y,jy(key[name][1]),c,key[name][0],name))
    elif name=="GND": p(f"M{UR+5} {y} H930",GND,2); gnd(930,y); t(950,y+4,"GND · bussed",MUT)
    elif name=="VCC": p(f"M{UR+5} {y} H930",RED,2); t(940,y+4,"+3V3 · bussed",RED)
    else: p(f"M{UR+5} {y} H916",DIM,2,"4 4"); nc(926,y); t(942,y+4,"n.c.",MUT)
# channel assignment: minimise crossings
X0,X1=990,1210
def cross(order):
    n=len(order); step=(X1-X0)/(n-1); cx={w:X0+k*step for k,w in enumerate(order)}
    cnt=0
    for a in order:
        for b in order:
            if a is b: continue
            s,tt=a[0],a[1]; lo,hi=sorted((b[0],b[1]))
            if cx[b]<cx[a] and lo<s<hi: cnt+=1   # a's first horizontal crosses b's vertical
            if cx[b]>cx[a] and lo<tt<hi: cnt+=1
            if abs(a[0]-b[1])<10 and cx[a]>cx[b]: cnt+=100   # a's first run overlaps b's last run
    return cnt
best=list(wires); bc=cross(best); random.seed(3)
for it in range(40000):
    cand=best[:]; i,j=random.sample(range(len(cand)),2); cand[i],cand[j]=cand[j],cand[i]
    c=cross(cand)
    if c<=bc: best,bc=cand,c
print("crossings",bc,file=sys.stderr)
step=(X1-X0)/(len(best)-1)
for k,(ys,yt,c,lab,name) in enumerate(best):
    x=X0+k*step
    p(f"M{UR+5} {ys} H{x:.0f} V{yt} H{JX-5}",c,2.5)
    t(JX-14,yt-6,lab,c,bold=True,anchor="end")
# ---------- J2
o.append(f'<rect x="{JX}" y="{jy(1)-22}" width="130" height="{jy(26)-jy(1)+44}" rx="6" fill="{BG}" stroke="{AMB}" stroke-width="1.5"/>')
o.append(f'<path d="M{JX} {jy(16)+25} H{JX+130}" stroke="#3A352F" stroke-width="1.5" stroke-dasharray="4 4"/>')
names={1:"GND",2:"K1",3:"K2",4:"K3",5:"K4",6:"K5",7:"K6",8:"PU",9:"TONE_W",10:"VOL_W",11:"POT+ / LED+",12:"POT−",13:"TONE sw",14:"LAMP_R",15:"LAMP_G",16:"LAMP_B",
       17:"EXP1",18:"EXP2",19:"EXP3",20:"EXP4",21:"EXP5",22:"EXP6",23:"A2",24:"A3",25:"+3V3",26:"GND"}
col={1:GND,9:AMB,10:AMB,11:RED,12:GND,14:RED,15:SDA,16:SCL,23:EXP,24:EXP,25:RED,26:GND}
for n in range(1,27):
    y=jy(n); c=col.get(n,AMB if n<=16 else EXP)
    pin(JX,y,c); t(JX+14,y+4,str(n),MUT,bold=True); t(JX+40,y+4,names[n],TXT,size=12,bold=True)
# other J2 pins: stubs + flags on the left
FX=1330
def lf(n,label,c,dash=None):
    y=jy(n); p(f"M{JX-5} {y} H{FX-0}",c,2,dash); flag(FX-6,y,label,c,right=False)
lf(1,"GND ★ at P",GND); lf(9,"U2 A1",AMB,"6 4"); lf(10,"U2 A0",AMB,"6 4"); lf(11,"+3V3",RED); lf(12,"GND ★ at U2",GND)
lf(14,"R1",RED); lf(15,"R2",SDA); lf(16,"R3",SCL); lf(23,"U2 A2",EXP,"6 4"); lf(24,"U2 A3",EXP,"6 4"); lf(25,"+3V3",RED); lf(26,"GND",GND)
# brackets
BX=JX+150
p(f"M{BX} {jy(1)-8} h8 V{jy(16)+8} h-8",AMB,1.5)
t(BX+20,jy(1)+4,"1–16",AMB,size=14,bold=True); 
for i,s in enumerate(["Hasler HM standard","HM 16-way plug","sits on 1–16","","keys, pots,","tone sw, LED"]): t(BX+20,jy(1)+26+i*17,s,MUT)
p(f"M{BX} {jy(17)-8} h8 V{jy(26)+8} h-8",EXP,1.5)
t(BX+20,jy(17)+4,"17–26",EXP,size=14,bold=True)
for i,s in enumerate(["expansion","Excelsior plug","uses all 26","","+6 inputs (PB)","+2 knobs (A2, A3)"]): t(BX+20,jy(17)+26+i*17,s,MUT)
t(JX,jy(26)+48,"unshrouded 2×13, cut from male strip",MUT)
t(JX,jy(26)+64,"mark pin 1 on board + plug",AMB)
t(UL,uy(19)+70,"keys are wired to the MCP pin their J2 strip lands on · the descriptor follows",MUT)
svg=f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg"><g font-family="JetBrains Mono, Menlo, monospace" font-size="11">'+"".join(o)+'</g></svg>'
open(sys.argv[1],"w").write(svg)
