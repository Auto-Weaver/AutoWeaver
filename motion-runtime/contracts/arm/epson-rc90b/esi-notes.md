# ESI Notes — Epson RC90 EtherCAT Slave Option Board

> Summary of facts extracted from `EPSN_RC90_ECT_V2.3_for_OMRON_rev2.xml`
> (Epson's ESI file for the RC90 EtherCAT slave option board, OMRON variant).
> Read this instead of grepping the 8640-line XML.

## Why this matters

The ESI file is the authoritative description of what the option board exposes
to the EtherCAT master. Our `contract.yaml` is derived from this ESI — every
PDO index, byte size, and CoE object index here must match what the contract
declares, otherwise the master and the slave will disagree about the wire
layout.

`contract.yaml` describes a single chosen PDO configuration (we use USINT
32-byte by default — see decision below). The ESI file shows the full menu
of options the board supports.

## Vendor / Product identity

| Field | Value |
|---|---|
| Vendor ID | `0x057E` (Epson) |
| Product Code | `0x00000003` |
| Revision No. | `0x00000001` |
| Slave name | `RC90_Slave` |
| Type tag | `Robot Controller` |
| Physics | `YY` (= 100BASE-TX on both ports) |

These can be used for precise slave matching (`vendor_id` + `product_code`)
once we move past the loose `name_contains` heuristic.

## SyncManagers

| SM | Direction | Start | Default | Max | Purpose |
|---|---|---|---|---|---|
| SM0 | Out | `0x1C00` | 192 B | 256 B | MailBox Out (CoE/SDO requests) |
| SM1 | In | `0x1E00` | 192 B | 256 B | MailBox In (CoE/SDO responses) |
| SM2 | Out | `0x1000` | 32 B | 256 B | **Outputs (master → slave)** |
| SM3 | In | `0x1600` | 32 B | 256 B | **Inputs (slave → master)** |

SM2/SM3 carry the process data; SM0/SM1 carry the CoE mailbox.

## Available PDO assemblies

The board offers two families of PDO mappings — pick **one** RxPDO and **one**
TxPDO; assemblies in the same family are mutually exclusive (the ESI uses
`<Exclude>` to express this).

### Family 1: USINT (byte-granular)

Every PDO entry is one USINT (unsigned 8-bit byte). The contents of the
process data are simply a contiguous byte region; how multi-byte values
(u16 / f32 / etc.) are packed inside that region is up to the master and
the controller-side program to agree on.

| Direction | PDO Index | Size |
|---|---|---|
| RxPDO | `0x1600` | 32 B (**default**) |
| RxPDO | `0x1601` | 64 B |
| RxPDO | `0x1602` | 128 B |
| RxPDO | `0x1603` | 128 B (alt) |
| RxPDO | `0x1604` | 256 B (uses two 128 B PDOs) |
| TxPDO | `0x1A00` | 32 B (**default**) |
| TxPDO | `0x1A01` | 64 B |
| TxPDO | `0x1A02` | 128 B |
| TxPDO | `0x1A03` | 128 B (alt) |
| TxPDO | `0x1A04` | 256 B (uses two 128 B PDOs) |

### Family 2: REAL (4-byte float-granular)

Every PDO entry is one REAL (IEEE754 32-bit float).

| Direction | PDO Index | Size | # REALs |
|---|---|---|---|
| RxPDO | `0x1605` | 32 B | 8 |
| RxPDO | `0x1606` | 64 B | 16 |
| RxPDO | `0x1607` | 128 B | 32 |
| RxPDO | `0x1608` | 256 B | 64 |
| TxPDO | `0x1A05` | 32 B | 8 |
| TxPDO | `0x1A06` | 64 B | 16 |
| TxPDO | `0x1A07` | 128 B | 32 |
| TxPDO | `0x1A08` | 256 B | 64 |

## Object dictionary mapping

All PDO entries map to two CoE objects (the board uses subindex-array
addressing rather than one object per byte):

| Direction | Object | Subindex range | Item type |
|---|---|---|---|
| Output (master writes) | `0x2100` | 1..N | USINT (8-bit) or REAL (32-bit) depending on PDO choice |
| Input (master reads)   | `0x2000` | 1..N | USINT or REAL |

So a 32-byte USINT RxPDO maps `0x2100:1` ... `0x2100:32`, each as one byte.
A 32-byte REAL RxPDO maps `0x2100:1` ... `0x2100:8`, each as a 4-byte REAL.
The same underlying storage; only the access granularity differs.

## What the ESI does NOT impose

These are also significant — they make the integration *simpler* than a
CiA402 servo drive:

- **No DC SYNC**. The ESI has no `<Dc>` section. The master does not need to
  configure distributed clocks on this slave; the cyclic-time-bias issue
  that bit us with Inovance SV660N (and forced the move from ethercrab to
  IgH) does **not** apply here.
- **No init SDO commands**. No `<InitCmds>` section — the master does not
  need to send any SDO writes during PREOP→SAFEOP.
- **`PdoAssign="0"` and `PdoConfig="0"`** on the CoE mailbox declaration —
  the PDO mapping is **fixed** by the ESI; you cannot re-assign or
  re-configure PDOs at runtime via SDO. The master must pick one of the
  predefined PDO assemblies listed above.

## Our choice: default USINT, 32 B each direction

For the LS6 contract we use **RxPDO `0x1600` (32 B USINT) + TxPDO `0x1A00`
(32 B USINT)** — the default.

Rationale:

- Our field set is heterogeneous: f32 (target_x..u), u16 (speed), u8
  (routine), bool/bit (trigger/done/busy). USINT-byte granularity lets us
  pack each at its natural width via byte offsets, which matches the
  "offset + type" form of `contract.yaml`.
- The REAL PDO would force everything to be 4-byte aligned; squeezing
  bits (trigger / done) and small ints (error code) into REAL slots is
  awkward (would need bit-level bit_cast hacks on both sides).
- 32 bytes is enough headroom for the current field set (~24 bytes used).
- It is the default — picking it means no SDO PDO-assign step is needed.

If we ever outgrow 32 B (more parameters, longer error strings, telemetry
streams), the natural upgrade is `0x1601` + `0x1A01` (USINT 64 B), still in
the same family.

## Controller-side access (SPEL+)

Within SPEL+ this slot of the option board appears as **Fieldbus Slave I/O**,
not via any EtherCAT-specific API. The data area is mapped into the
controller's general-purpose I/O port space starting at **bit 512** by
default (both Input and Output sides). Source: Epson Fieldbus I/O manual.

### Port-number arithmetic

```
base_bit  = 512
base_byte = 512 / 8  = 64
base_word = 512 / 16 = 32
```

| What | SPEL+ port |
|---|---|
| byte at fieldbus offset N | `64 + N` |
| word (u16) at fieldbus offset N | `32 + N/2` (N must be even) |
| REAL (f32) at fieldbus offset N | `32 + N/2` (N must be even, N%4==0 recommended) |
| bit K of byte N | `512 + 8*N + K` |

### Access commands

| Direction | Granularity | Read (master → slave) | Write (slave → master) |
|---|---|---|---|
| Bit | 1 bit | `Sw(bitnum)` | `On bitnum` / `Off bitnum` |
| Byte | 8 bits | `In(port)` | `Out port, val` |
| Word | 16 bits | `InW(port)` | `OutW port, val` |
| REAL | 32 bits | `InReal(port)` | `OutReal port, val` |

Example — read `target_x` (f32 at byte offset 0):
```
Real target_x
target_x = InReal(32)        ' base_word + 0/2 = 32
```

Example — read `trigger` bit (byte 19, bit 0):
```
Integer trigger
trigger = Sw(512 + 19*8 + 0)  ' = Sw(664)
```

Example — write `done` bit (byte 0 bit 0) to 1:
```
On (512 + 0*8 + 0)           ' = On 512
```

## Byte order

Not explicitly documented in the Epson manual. Empirical convention:
**little-endian** (matches IgH's `EC_READ_LE_*` family and most x86 master
implementations). Confirm on bring-up by writing `1.0f` from the master and
checking that SPEL+ `InReal(32)` reads back `1.0`. If wrong, the wire bytes
will be `3F 80 00 00` (big-endian) instead of `00 00 80 3F` (little-endian).

## Configuration on the controller side

The data-area size (Input bytes / Output bytes) and base bit address are
set in EPSON RC+ under "Setup → System Configuration → Fieldbus I/O", not
in the SPEL+ source. When commissioning a new robot:

1. Open RC+ → System Configuration → Fieldbus I/O
2. Set both Input and Output to 32 bytes (match `contract.yaml`)
3. Leave base address at default (bit 512)
4. Save, restart controller

Any mismatch between RC+ configuration and `contract.yaml` will cause the
master to either over/under-allocate PDO bytes (link comes up but partial
data) or fail to enter OP. Track this as part of the commissioning
checklist in this directory's `README.md`.

## Sources

- This ESI file: `EPSN_RC90_ECT_V2.3_for_OMRON_rev2.xml` (Epson, OMRON-tested variant)
- Epson Fieldbus I/O Manual: https://files.support.epson.com/far/docs/epson_fieldbusio_manual-rc700a_rc90_t%28r12%29.pdf
- Alternative Epson hosting: https://download.epson.biz/robots/ww/data/pdf/en/FieldbusIO_RC700_RC90.pdf
- SPEL+ Language Reference: https://download.epson.biz/robots/ww/data/pdf/en/SPEL%2BRef80_RC700.pdf
- Epson Fieldbus electronic files (ESI archive): https://download.epson.biz/robots/fieldbus/FieldbusElectronicFiles.zip
- IgH EtherCAT process-data API (LE byte order): https://docs.etherlab.org/ethercat/1.6/doxygen/group__ApplicationInterface.html
