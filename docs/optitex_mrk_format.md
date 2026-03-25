# Optitex MRK File Format — Reverse Engineering Notes

## Status: 80% decoded. Saved as fallback for direct MRK generation if blank-template approach proves insufficient.

## Primary Approach: Blank MRK Template

The production workflow uses **blank MRK templates** (customer-provided, created from PDS via Optitex Mark).
MarkerMind injects nesting placements into the XML section — no binary encoding needed.

See: `docs/optitex_mrk_template_writer.md` (to be created with the writer implementation)

---

## File Signature

All Optitex binary files (MRK, PDS, MRKML) share the RWF container format:
- **Magic bytes**: `DC 54 EF 12` (little-endian `0x12EF54DC`)
- **Header**: 28 bytes fixed

```
Offset 0-3:   DC 54 EF 12     (magic)
Offset 4-7:   uint32           (varies per file)
Offset 8-11:  uint32           (varies per file)
Offset 12-15: uint32           (modification hash, reused as 0x55/0x8c field)
Offset 16-19: uint32           (varies)
Offset 20-23: uint32           (varies)
Offset 24-27: uint32           (varies)
```

After header: `"Default Table"` (13 bytes) at offset 28-44.

## File Variants

| Variant | Byte 45 | Description | Optitex Versions |
|---------|---------|-------------|-----------------|
| XML | `0x3C` (`<`) | Embedded XML + optional JPEG thumbnail + binary data | 12.3, 15.6, 17.1, 23.1, 25.1 |
| Binary-only | `0x35` (`5`) | Pure binary data, no XML | Pre-v18 (VTG factory files) |

Both variants have the same binary section structure after the XML (if present).

## MRK File Section Map

```
[0-27]        28-byte header (magic + 6 uint32 fields)
[28-44]       "Default Table" (13 bytes)
[45]          Format marker:
                0x3C = XML variant: <?xml>...<MARKER>...</MARKER> + optional JPEG
                0x35 = Binary-only: starts binary data section
[var]         Binary data section (marker: 35 04 33 48)
                Sub-sections: WV, GE, CB, QU, ST, DT
                Contains: marker layout visualization / JPEG thumbnail
[var]         Sentinel: FF FF FF FF 00 00
[var]         Style section (marker: 37 05 2C C1)
                Style name, size table (with RGB colors), Variations
[var]         Piece records (U#####K format)
                Metadata, material, descriptions, placement data
[var]         File path references (PDS source)
[var]         Contour/geometry data (6F 12 70 86 vertex records)
                Per-piece polygon outlines, graded sizes
[EOF]
```

## FLENT Type System (from C++ RTTI)

| FLENT Class | Binary Type Code | Payload Format |
|-------------|-----------------|----------------|
| `FLENT_STR` | `0x05`, `0x10` | `[1-byte len] [0x20] [chars]` |
| `FLENT_BSTR` | `0x08` | `[2-byte LE len] [bytes]` |
| `FLENT_INT` | `0xc1` | `[4-byte LE int32]` |
| `FLENT_LNG` | `0xc0` | `[4-byte LE uint32]` |
| `FLENT_FLT` | `0x28` | `[1-byte value]` or array |
| `FLENT_DBL` | embedded | `[8-byte IEEE 754 double]` |
| `FLENT_BLK` | `0x04` | `[1-byte len] [data]` |

Record encoding: `[field_id: 1 byte] [type_code: 1 byte] [payload]`
Field IDs are **context-dependent** (local to block type).

## Confirmed Binary Field-Tag Mappings

| Binary Tag | Type | RWF Field | Example |
|-----------|------|-----------|---------|
| `0x36` | STR | `RWF_USE_SIZES_TABLE` | "Default Table" |
| `0x14` | STR | Size name | "M", "XL" |
| `0x2c` | UINT32 | Size color | RGBA packed |
| `0x18` | INT32 | Size internal ID | Sequential |
| `0x39` | STR | Variation name | "Variation1" |
| `0x6e` | BSTR | `RWF_PIECE_UNIQUE` | "U00001" |
| `0x4b` | BSTR | Piece description | Localized text |
| `0x4c` | BSTR | `RWF_PIECE_MATERIAL` | "101" |
| `0x05` | STR | `RWF_PIECE_CODE` | "BS1" |
| `0x55` | HASH | Modification hash | Matches header[12:16] |
| `0x59` | BLOCK | Instance/placement data | Per-piece |

## RWF_MRK Field Dictionary (203 fields extracted from PdsExport.exe)

### Key Placement Fields
- `RWF_MRK_EL_ROTATION_ANGLE` — double, piece rotation
- `RWF_MRK_EL_PLACED` — int32, placed flag (0/1)
- `RWF_MRK_EL_PLACED_MIRROR` — int32, flip/mirror flag
- `RWF_MRK_EL_ROOT_ANGLE` — double
- `RWF_MRK_EL_ROOT_MIRROR` — int32
- `RWF_MRK_EL_SCALEX` / `SCALEY` — doubles
- `RWF_MRK_EL_CONTOUR_ORIGINAL` — block, polygon vertices
- `RWF_MRK_EL_CONTOUR_BUFFER` — block, buffered polygon
- `RWF_MRK_EL_SIZEINFO_ID` — int32
- `RWF_MRK_EL_ID` — int32

### Key Marker Fields
- `RWF_MRK_SPREADING_WIDTH` — double, fabric width
- `RWF_MRK_SPREADING_LENGTH` — double, marker length
- `RWF_MRK_SPREADING_XMIN/XMAX/YMIN/YMAX` — doubles, bounding box
- `RWF_MRK_SPREADING_LAYERS` — int32, plies

### Key Design Fields
- `RWF_MRK_DESIGN_FILE_NAME` — string, PDS source
- `RWF_MRK_DESIGN_MATERIAL` — string, material code(s)
- `RWF_MRK_DESIGN_STYLE_SET_NAME` — string

### Key Instance Fields
- `RWF_MRK_INSTANCE` — block, single instance
- `RWF_MRK_INSTANCE_PIECE_ID` — int32
- `RWF_MRK_INSTANCE_STATUS` — int32
- `RWF_MRK_INSTANCE_BUNDLE_NUM` — int32

## Piece Polygon Vertex Encoding (Binary Section)

14-byte vertex records with marker `6F 12 70 86`:
```
6F 12 70 86    4 bytes: constant marker
XX XX XX       3 bytes: X coordinate (24-bit LE)
FF             1 byte: X high byte (0x19-0x1b)
YY YY YY       3 bytes: Y coordinate (24-bit LE)
GG             1 byte: Y high byte (0x19-0x1a)
00 00          2 bytes: padding
```

Coordinate = `low24 | (high_byte << 24)`
Scale: ~1,699,386 units per inch (~66,904 units per mm)

## PDS-MRK Connection

- PDS contains piece unique IDs: `U00001` through `U00056`
- MRK references same IDs with `K` suffix: `U00001K`
- Piece IDs are global (not per-marker)
- Material determines which pieces appear in which MRK file:
  - Fabric 101: U00001-U00008 (pieces 1-8)
  - Fabric 201: U00009-U00016 (pieces 9-16, with U00021 instead of U00011)
  - Fabric 301: U00042, U00043, U00056 (pieces 17-19)

## XML Schema (from XML-variant MRK files)

```xml
<MARKER>
  <OPTITEX>17.1.633.0</OPTITEX>
  <NEST><NAME>Standard Quick Nesting</NAME></NEST>
  <UNITS LINEAR="inch" SQUARE="sq.yd" />
  <NAME>marker-name</NAME>
  <LENGTH>169.2979</LENGTH>
  <WIDTH>50.0000</WIDTH>
  <EFFICIENCY>86.2008</EFFICIENCY>
  <PLACED_ON_TABLE>24</PLACED_ON_TABLE>
  <STYLE>
    <NAME>style-name</NAME>
    <MATERIAL>S,_SI</MATERIAL>
    <SIZE>
      <NAME>M</NAME>
      <NB_OF_SETS>1</NB_OF_SETS>
      <PIECE>
        <NAME>piece-name</NAME>
        <MATERIAL>101</MATERIAL>
        <GEOM_INFO SIZE_X="28.8" SIZE_Y="18.9" AREA="0.36" PERIMETER="85.9" />
        <NESTING_ENABLES ROTATION="none" FLIP="no" FOLD="none" />
        <PLACED>
          <POSITION X-CENTER="34.03" Y-CENTER="11.00" ANGLE="0.0" FLIP="no" />
        </PLACED>
      </PIECE>
    </SIZE>
  </STYLE>
</MARKER>
```

### Blank MRK vs Nested MRK (XML diff)

Changes when nesting is applied to a blank:
1. `LENGTH`: 0.0000 → actual marker length
2. `EFFICIENCY`: 0.0000 → utilization %
3. `PLACED_ON_TABLE`: 0 → total placements count
4. Per-size `NB_OF_SETS`: 0 → bundle count for that size
5. Unused sizes: `<PIECE>` elements removed
6. Each placed piece gains: `<PLACED><POSITION X-CENTER="..." Y-CENTER="..." ANGLE="..." FLIP="..." /></PLACED>`

### Placement Units
- X-CENTER, Y-CENTER: in the declared LINEAR unit (inches or mm)
- X runs along marker **length**, Y along marker **width**
- ANGLE: 0.0 or -180.0 (or 180.0)
- FLIP: "no" or "down"

## Coordinate System
- Internal binary units: ~254 per inch (10 per mm)
- XML placement units: declared in `<UNITS LINEAR="inch|cm|mm" />`

## Files Analyzed
- 19 MRK files from VTG (25528XX, fabrics 101/201/301)
- 4 MRK files from other vendors (S04 shirt, SP22 jacket — blank + nested pairs)
- 1 PDS file (25528XX.pds)
- Executables: PdsExport.exe, Mark.exe, MarkerDBT.dll, DBT10.dll, Import10.dll

## Remaining Unknowns (for direct binary generation)
- Exact binary encoding of piece placement coordinates (not simple IEEE 754)
- Full field_id → enum_index mapping (context-dependent)
- Preview bitmap/JPEG generation
- Whether Optitex validates binary section checksums
