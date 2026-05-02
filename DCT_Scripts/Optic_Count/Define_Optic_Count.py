#
#   The File contains functions to be used in Optic counting Py script
#

import os
import re
import pandas as pd
import OpticType
from cutsheet_preprocessor import (
    classify_status as _classify_status,
    COMPLETE, HUMAN_VERIFIED, LLDP_PASSED,
)

_IN_SERVICE_CANONICAL = {COMPLETE, HUMAN_VERIFIED, LLDP_PASSED}

file_type_for_cutsheet = "cutsheet"
file_type_for_ib = "infini_band"
file_type_for_roce = "roce"

COL_WIDTH = 35
TABLE_WIDTH = COL_WIDTH * 2 + 3

# Sheet tabs we skip when scanning a workbook. Matches backup copies, duplicate
# snapshots, Excel default names (Sheet1, Sheet18), and legend/overhead reference
# tabs. Applied to the casefolded, stripped tab name.
_SKIP_SHEET_PATTERNS = (
    re.compile(r"\bbackup\b"),
    re.compile(r"^copy of\b"),
    re.compile(r"^sheet\d+$"),           # Sheet1, Sheet18, etc.
    re.compile(r"^legend(-|_|$)"),       # LEGEND-NET, LEGEND-GPU, LEGEND-CPU
    re.compile(r"^overhead$"),
    re.compile(r"\bold\b|\barchive\b|\bdeprecated\b"),
)


def _should_skip_sheet(name: str) -> bool:
    """True if a sheet tab is a backup/copy/junk tab we should not parse."""
    if not name:
        return True
    n = name.strip().casefold()
    return any(pat.search(n) for pat in _SKIP_SHEET_PATTERNS)


def _active_sheet_names(xls) -> list:
    """Return the subset of sheet names worth parsing (filters backups/junk)."""
    return [s for s in xls.sheet_names if not _should_skip_sheet(s)]


# ---------------------------------------------------------------------------
# Per-request Excel cache.  Avoids re-parsing the same 50k-row xlsx 3-4 times
# during a single upload.  Call clear_excel_cache() when the request is done.
# ---------------------------------------------------------------------------
_XLS_CACHE: dict = {}        # filepath -> pd.ExcelFile
_DF_CACHE: dict = {}         # (filepath, sheet, header_row) -> DataFrame


def _excel_file_path(xls):
    """Extract the file path from a pd.ExcelFile, regardless of pandas version."""
    # pandas < 3.0 exposes .io; 3.0+ may not.
    for attr in ("io", "_io", "path"):
        val = getattr(xls, attr, None)
        if isinstance(val, (str, os.PathLike)):
            return str(val)
    raise TypeError(f"Cannot determine file path from {type(xls).__name__}")


def _cached_excel_file(filepath):
    """Return a pd.ExcelFile, reusing a cached instance when possible."""
    key = os.path.realpath(filepath)
    if key not in _XLS_CACHE:
        _XLS_CACHE[key] = pd.ExcelFile(filepath, engine="openpyxl")
    return _XLS_CACHE[key]


def _cached_read_sheet(filepath_or_xls, sheet_name, header=0, nrows=None):
    """Read a single sheet, returning a cached copy if we already parsed it."""
    if isinstance(filepath_or_xls, pd.ExcelFile):
        fpath = os.path.realpath(_excel_file_path(filepath_or_xls))
        xls = filepath_or_xls
    else:
        fpath = os.path.realpath(filepath_or_xls)
        xls = _cached_excel_file(fpath)
    # nrows reads are never cached (used for header-sniffing only)
    if nrows is not None:
        return pd.read_excel(xls, sheet_name=sheet_name, header=header, nrows=nrows)
    key = (fpath, sheet_name, header)
    if key not in _DF_CACHE:
        _DF_CACHE[key] = pd.read_excel(xls, sheet_name=sheet_name, header=header)
    return _DF_CACHE[key].copy()


def clear_excel_cache():
    """Drop all cached ExcelFile handles and DataFrames."""
    _XLS_CACHE.clear()
    _DF_CACHE.clear()


def _normalize_cell(value):
    if pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.casefold() in {"nan", "-", "none", "null"}:
        return None
    return text


def _is_lldp_passed(raw_status):
    """True if the status value represents an LLDP-verified in-service connection."""
    normalized = " ".join(str(raw_status).split())
    return _classify_status(normalized) == LLDP_PASSED


def _is_in_service_status(raw_status):
    """True if the status value should count as in service for workbook summaries."""
    normalized = " ".join(str(raw_status).split())
    return _classify_status(normalized) in _IN_SERVICE_CANONICAL


def _is_csv(input_file):
    return str(input_file).lower().endswith(".csv")


def _normalize_columns(df):
    """Collapse whitespace (including newlines) in column headers to single spaces."""
    df.columns = [re.sub(r'\s+', ' ', str(c)).strip() for c in df.columns]
    return df


def _read_cutsheet_df(input_file):
    """Return (DataFrame, source_sheet_name) for both .csv and .xlsx inputs."""
    if _is_csv(input_file):
        df = pd.read_csv(input_file)
        return _normalize_columns(df), "CUTSHEET"
    xls = _cached_excel_file(input_file)
    sheet_name = _find_cutsheet_sheet_name(xls)
    if not sheet_name:
        return None, None
    df = _cached_read_sheet(xls, sheet_name=sheet_name)
    return _normalize_columns(df), sheet_name


def _find_cutsheet_sheet_name(xls):
    # Prefer explicit cutsheet tab name, case-insensitive. Skip backup copies
    # like "123125 Backup CUTSHEET - 3 tier".
    for sheet_name in xls.sheet_names:
        if _should_skip_sheet(sheet_name):
            continue
        if sheet_name.strip().casefold() == "cutsheet":
            return sheet_name

    # Fallback: identify a cutsheet-like tab by required columns (still skipping
    # backups so we don't load stale data).
    required_cols = {"A-OPTIC", "Z-OPTIC", "A-SIDE LOCODE", "Z-SIDE LOCODE"}
    for sheet_name in xls.sheet_names:
        if _should_skip_sheet(sheet_name):
            continue
        try:
            df = _cached_read_sheet(xls, sheet_name=sheet_name, nrows=0)
        except Exception:  # noqa: BLE001
            continue
        cols = {str(c).strip() for c in df.columns}
        if required_cols.issubset(cols):
            return sheet_name

    return None


##### function to get user input:
def menu():
    file_list = []
    while True:
        file_list.append(input("Enter file path:"))
        if "xlsx" not in file_list[-1]:
            file_list.pop()
            print("File not added, must be in xlsx format.")
        more = input("Do you want to add more file?(y/n):")
        while more != "y" and more != "n":
            more = input("Only inputs are accepted (y/n):")
            print(more)
        if more == "y":
            continue
        elif more == "n":
            break
    return file_list

##### This Function is to determine what type of file is being counted.
def get_file_type(input_file):
    if not os.path.exists(input_file):
        raise FileNotFoundError(f"File not found: {input_file}")

    # CSV files are always treated as cutsheets (single-sheet export).
    if _is_csv(input_file):
        try:
            df = pd.read_csv(input_file, nrows=0)
        except (FileNotFoundError, OSError) as e:
            raise ValueError(f"Could not open file '{os.path.basename(input_file)}': {e}")
        required_cols = {"A-OPTIC", "Z-OPTIC", "A-SIDE LOCODE", "Z-SIDE LOCODE"}
        if required_cols.issubset({str(c).strip() for c in df.columns}):
            return file_type_for_cutsheet
        return "Unsupported File"

    try:
        xls = _cached_excel_file(input_file)
    except (FileNotFoundError, ValueError, OSError) as e:
        raise ValueError(f"Could not open file '{os.path.basename(input_file)}': {e}")
    sheet_names = xls.sheet_names
    cutsheet_sheet = _find_cutsheet_sheet_name(xls)
    if cutsheet_sheet:
        return file_type_for_cutsheet
    if any("pull schedule" in name.casefold() for name in sheet_names):
        return file_type_for_ib
    if "roce" in input_file.casefold():
        return file_type_for_roce
    return "Unsupported File"

##### Logic for counting optics found in a cutsheet based file
# When sort_by_status=True, splits into (in_service, not_in_service) using the STATUS column.
# In Service = any verified/completed status, Not In Service = anything else.
# When sort_by_status=False, all optics go into in_service and not_in_service is empty.
def count_cutsheet(input_file, sort_by_status=False):
    cutsheet, _sheet_name = _read_cutsheet_df(input_file)
    if cutsheet is None:
        return [], []
    breakout_ports_in = []
    breakout_ports_not = []
    in_service_list = []
    not_in_service_list = []
    a_string = 'A'
    z_string = 'Z'

    status_col = None
    if sort_by_status:
        for col in cutsheet.columns:
            if str(col).lower() == 'status':
                status_col = col
                break

    for index, row in cutsheet.iterrows():
        if status_col and _is_in_service_status(row[status_col]):
            optic_list = in_service_list
            breakout_ports = breakout_ports_in
        else:
            optic_list = not_in_service_list if sort_by_status else in_service_list
            breakout_ports = breakout_ports_not if sort_by_status else breakout_ports_in

        #A side
        process_cutsheet_row(row, breakout_ports, optic_list, a_string)
        #Z side
        process_cutsheet_row(row, breakout_ports, optic_list, z_string)

    return in_service_list, not_in_service_list

##### Logic for counting optics found in a infini band based file
# When sort_by_status=True, splits into (in_service, not_in_service) using column L.
# In Service = "Complete", Not In Service = anything else.
# When sort_by_status=False, all optics go into in_service and not_in_service is empty.
def count_infini_band(input_file, sort_by_status=False):
    # Skip backup/copy tabs up front so we don't pay the parse cost on dead weight.
    xls = _cached_excel_file(input_file)
    active_sheets = _active_sheet_names(xls)
    infini_band = {s: _cached_read_sheet(input_file, sheet_name=s) for s in active_sheets}
    in_service_list = []
    not_in_service_list = []
    sheets_with_optic = []
    node_sheets = []
    ufm_sheets = []
    node_count = 0

    for tab_name, sheet_data in infini_band.items():
        if "Optic Type" in sheet_data:
            sheets_with_optic.append(tab_name)
        if "node" in tab_name.casefold():
            node_sheets.append(tab_name)
        if "ufm" in tab_name.casefold():
            ufm_sheets.append(tab_name)

#Counts optic Type field for Core and Leaf pull schedules and pulls unique optics.
    for active_sheet in sheets_with_optic:
        sheet_df = _cached_read_sheet(input_file, sheet_name=active_sheet)
        for index, row in sheet_df.iterrows():
            if "Optic Type" in row.index:
                value = row["Optic Type"]
                if pd.notna(value):
                    if sort_by_status:
                        status_val = str(row.iloc[11]).strip() if len(row) > 11 else ""
                        optic_list = in_service_list if status_val == "Complete" else not_in_service_list
                    else:
                        optic_list = in_service_list
                    put_optic_in_list(optic_list, value)

#Count optics in Node to Leaf Pull Schedules They don't have Optic type so using Generic IB Node Optic name
    for active_sheet in node_sheets:
        sheet_df = _cached_read_sheet(input_file, sheet_name=active_sheet)
        if sort_by_status:
            in_node_count = 0
            not_node_count = 0
            for index, row in sheet_df.iterrows():
                if "IBP" in row.index and pd.notna(row["IBP"]):
                    status_val = str(row.iloc[0]).strip() if len(row) > 0 else ""
                    if status_val == "Complete":
                        in_node_count += 1
                    else:
                        not_node_count += 1
            #Doubling to account for both sides of the link
            in_node_count *= 2
            not_node_count *= 2
            for i in range(in_node_count):
                put_optic_in_list(in_service_list, "IB Node Optic")
            for i in range(not_node_count):
                put_optic_in_list(not_in_service_list, "IB Node Optic")
        else:
            node_count += sheet_df["IBP"].notna().sum()
    #Doubling node_count to account for both sides of the link. This assumes that both sides use the same optic as optic type is not provided in the source document
    if not sort_by_status:
        node_count = node_count * 2
        for i in range(1, node_count + 1):
            put_optic_in_list(in_service_list, "IB Node Optic")

#Counting Optics UFM tabs contain two tables and don't match other formating styles
    for active_sheet in ufm_sheets:
        sheet_df = _cached_read_sheet(input_file, sheet_name=active_sheet)
        if sort_by_status:
            for index, row in sheet_df.iterrows():
                twin_count = (row == "Twin Port OSFP").sum()
                if twin_count > 0:
                    status_val = str(row.iloc[16]).strip() if len(row) > 16 else ""
                    optic_list = in_service_list if status_val == "Complete" else not_in_service_list
                    for i in range(twin_count):
                        put_optic_in_list(optic_list, "Twin Port OSFP")
        else:
            ufm_count = sheet_df.eq("Twin Port OSFP").sum().sum()
            for i in range(1, ufm_count + 1):
                put_optic_in_list(in_service_list, "Twin Port OSFP")

    return in_service_list, not_in_service_list

##### Logic for counting optics found in a ROCE based file
# When sort_by_status=True, splits into (in_service, not_in_service) using the STATUS column.
# In Service = any verified/completed status, Not In Service = anything else.
# When sort_by_status=False, all optics go into in_service and not_in_service is empty.
def count_roce(input_file, sort_by_status=False):
    # Pre-filter backup/copy tabs so we don't read them at all.
    xls = _cached_excel_file(input_file)
    active_sheets = _active_sheet_names(xls)
    roce_df_dict = {s: _cached_read_sheet(input_file, sheet_name=s) for s in active_sheets}
    in_service_list = []
    not_in_service_list = []
    a_string = 'A'
    z_string = 'Z'

    for roce_sheet_name, roce_df in roce_df_dict.items():
        roce_occupied_ports_in = []
        roce_occupied_ports_not = []
        if "backup" not in roce_sheet_name.lower():
            status_col = None
            if sort_by_status:
                for col in roce_df.columns:
                    if str(col).lower() == 'status':
                        status_col = col
                        break

            for index, row in roce_df.iterrows():
                if status_col and _is_in_service_status(row[status_col]):
                    optic_list = in_service_list
                    roce_occupied_ports = roce_occupied_ports_in
                else:
                    optic_list = not_in_service_list if sort_by_status else in_service_list
                    roce_occupied_ports = roce_occupied_ports_not if sort_by_status else roce_occupied_ports_in

                # node to tier-0
                if "node to tier-0" in roce_sheet_name.lower():
                    # A side
                    process_roce_row(row, roce_occupied_ports, optic_list, str(row[a_string + '-PORT']), a_string)
                    # Z side
                    process_roce_row(row, roce_occupied_ports, optic_list, str(row[z_string + '-PORT']), z_string)
                # tier-0 to tier-1
                elif "tier-0 to tier-1" in roce_sheet_name.lower():
                    # In tier-0 to tier-1 there are 2 cables going to the same optic
                    # Labled as s1 & s2 or s3 & s4 the s# must be strriped off the port to get and accurate count
                    # A side
                    process_roce_row(row, roce_occupied_ports, optic_list, str((row[a_string + '-PORT'])[:-2]), a_string)
                    # Z side
                    process_roce_row(row, roce_occupied_ports, optic_list, str((row[z_string + '-PORT'])[:-2]), z_string)

    return in_service_list, not_in_service_list


def check_if_breakout_port_occupied(loc, port,breakout_ports_input):
    fullname = str(loc) + str(port)
    for occupied_breakout in breakout_ports_input:
        if fullname == occupied_breakout:
            return True
    return False

def check_if_roce_port_occupied(loc, port, connector, ports_input):
    fullname = str(loc) + str(port) + str(connector)
    for occupied_port in ports_input:
        if fullname == occupied_port:
            return True
    return False

# Sanitize values read from untrusted Excel cells before storing or displaying them.
# Strips leading formula-injection characters (=, +, -, @) that Excel would execute
# if this data were ever written back to a spreadsheet or CSV.
# For future AI agent use: extend this function to detect and strip prompt-injection
# patterns (e.g. lines starting with "Ignore", "System:", etc.) before passing cell
# values to a language model.
_FORMULA_INJECTION_CHARS = ('=', '+', '-', '@')
_MAX_CELL_VALUE_LEN = 200

# ---------------------------------------------------------------------------
# Column-name candidates for SITE-HOSTS and CUTSHEET device columns.
# Listed in priority order — first match wins.
# ---------------------------------------------------------------------------
_SITE_HOSTS_HOSTNAME_COLS = ("DNS-A-RECORD", "HOSTNAME", "Hostname", "Device Name")
_SITE_HOSTS_MODEL_COLS    = ("NETBOX MODEL", "MODEL", "Model", "Device Model")
_SITE_HOSTS_STATUS_COLS   = ("Status", "STATUS", "Install Status")

_CUTSHEET_A_DEVICE_COLS = (
    "A-SIDE-DNS-NAME", "A-SIDE DEVICE NAME", "A SIDE DEVICE", "A-SIDE DEVICE",
)
_CUTSHEET_Z_DEVICE_COLS = (
    "Z-SIDE-DNS-NAME", "Z-SIDE DEVICE NAME", "Z SIDE DEVICE", "Z-SIDE DEVICE",
)

def sanitize_cell_value(value: str) -> str:
    value = value.strip()
    while value and value[0] in _FORMULA_INJECTION_CHARS:
        value = value[1:].strip()
    return value[:_MAX_CELL_VALUE_LEN]

def _first_col(df, candidates):
    """Return the first candidate column name present in df, or None."""
    cols = set(df.columns)
    for c in candidates:
        if c in cols:
            return c
    return None


def _read_site_hosts_tab(input_file):
    """Return a normalized DataFrame for the SITE-HOSTS tab, or None if absent."""
    if _is_csv(input_file):
        return None
    xls = _cached_excel_file(input_file)
    for sheet_name in xls.sheet_names:
        if _should_skip_sheet(sheet_name):
            continue
        if sheet_name.strip().casefold().replace(" ", "-") in ("site-hosts", "sitehosts", "site_hosts"):
            df = _cached_read_sheet(input_file, sheet_name=sheet_name)
            return _normalize_columns(df)
    return None


def put_optic_in_list(optic_list_input, optic_type_input):
    optic_type_input = sanitize_cell_value(str(optic_type_input))
    if not optic_type_input or len(optic_type_input) < 2:
        return
    for optic in optic_list_input:
        if optic.compare_name(optic_type_input):
            optic.add()
            return
    # Store the canonical name as uppercase so case variants merge cleanly
    to_add = OpticType.OpticType(optic_type_input.upper(), 1)
    optic_list_input.append(to_add)

def process_roce_row(row, roce_occupied_ports_in, optic_list_to_return_in, port, side):
    # roce does not have breakouts, check line is not empty and optic exists
    if str(row[side + '-SIDE-DNS-NAME']) != "nan" and str(row[side + '-OPTIC']) != "nan":
        if not check_if_roce_port_occupied(row[side + '-LOC:CAB:RU'], port, row[side + '-CONNECTOR'], roce_occupied_ports_in):
            put_optic_in_list(optic_list_to_return_in, str(row[side + '-OPTIC']))
            roce_occupied_ports_in.append(str(row[side + '-LOC:CAB:RU']) + port + str(row[side + '-CONNECTOR']))

def process_cutsheet_row(row,breakout_ports_in, optic_list_to_return_in, side ):
    # Link does not have Breakout on A side and has an optic
    if str(row[side + '-SIDE LOCODE']) != "nan" and str(row[side + '-BREAKOUT LOC:CAB:RU']) == "nan" and str(row[side + '-OPTIC']) != "nan":
        put_optic_in_list(optic_list_to_return_in, str(row[side + '-OPTIC']))

    # Link has Breakout on A side, does not check Optic column(Optic is assumed to exist)
    elif str(row[side + '-SIDE LOCODE']) != "nan" and str(row[side + '-BREAKOUT LOC:CAB:RU']) != "nan":
        if not check_if_breakout_port_occupied(row[side + '-LOC:CAB:RU'], row[side + '-PORT'], breakout_ports_in):
            put_optic_in_list(optic_list_to_return_in, str(row[side + '-OPTIC']))
            breakout_ports_in.append(str(row[side + '-LOC:CAB:RU']) + str(row[side + '-PORT']))

##### Logic for counting unique IB switch devices across all Pull Schedule tabs in an InfiniBand file.
##### Source and Destination columns are read per sheet. Duplicate device names across sheets are counted once.
##### Devices are generically labelled "IB Switch" since IB files do not provide model information.
# When sort_by_status=True, a device is In Service if it appears in any row where column L == "Complete".
# When sort_by_status=False, all devices go into in_service and not_in_service is empty.
def count_devices_infini_band(input_file, sort_by_status=False):
    xls = _cached_excel_file(input_file)
    all_devices = set()
    in_service_devices = set()
    # Words that appear as embedded secondary headers in some sheets — not real device names
    _JUNK_WORDS = {"status", "source", "destination", "n/a", "nan"}

    for sheet_name in xls.sheet_names:
        if sheet_name.upper() == "OVERHEAD":
            continue
        if "pull schedule" not in sheet_name.casefold():
            continue

        # Read with default header; strip whitespace from column names
        df = _cached_read_sheet(xls, sheet_name=sheet_name)
        df.columns = [str(c).strip() for c in df.columns]

        # Some sheets (e.g. UFM) have a merged title row — retry with header on row 1
        if "Source" not in df.columns or "Destination" not in df.columns:
            df = _cached_read_sheet(xls, sheet_name=sheet_name, header=1)
            df.columns = [str(c).strip() for c in df.columns]

        if "Source" not in df.columns or "Destination" not in df.columns:
            continue

        for _, row in df.iterrows():
            for col in ["Source", "Destination"]:
                val = str(row[col]).strip() if pd.notna(row[col]) else ""
                # Skip blanks, values with spaces (label/status junk rows), and known header words
                if val and " " not in val and val.lower() not in _JUNK_WORDS:
                    all_devices.add(val)
                    if sort_by_status:
                        status_val = str(row.iloc[11]).strip() if len(row) > 11 else ""
                        if status_val == "Complete":
                            in_service_devices.add(val)

    in_service_list = []
    not_in_service_list = []
    if sort_by_status:
        not_in_service_devices = all_devices - in_service_devices
        if in_service_devices:
            in_service_list.append(OpticType.OpticType("IB Switch", len(in_service_devices)))
        if not_in_service_devices:
            not_in_service_list.append(OpticType.OpticType("IB Switch", len(not_in_service_devices)))
    else:
        if all_devices:
            in_service_list.append(OpticType.OpticType("IB Switch", len(all_devices)))

    return in_service_list, not_in_service_list


##### Logic for counting unique ROCE devices by location ID (A-LOC:CAB:RU / Z-LOC:CAB:RU) and model (A-MODEL / Z-MODEL).
##### Rows sharing the same location ID are the same physical device and are only counted once.
##### Sheets containing "backup", "dup", or "duplicate" (any variation) in the name are skipped.
# When sort_by_status=True, a device location is In Service if any row with that
# location has a verified/completed status.
# When sort_by_status=False, all devices go into in_service and not_in_service is empty.
def count_devices_roce(input_file, sort_by_status=False):
    xls = _cached_excel_file(input_file)
    active_sheets = _active_sheet_names(xls)
    roce_df_dict = {s: _cached_read_sheet(xls, sheet_name=s) for s in active_sheets}
    seen_locations = set()
    in_service_locs = set()
    in_service_list = []
    not_in_service_list = []

    # First pass: find which locations have at least one in-service row
    if sort_by_status:
        for sheet_name, df in roce_df_dict.items():
            status_col = None
            for col in df.columns:
                if str(col).lower() == 'status':
                    status_col = col
                    break
            if not status_col:
                continue
            for _, row in df.iterrows():
                if _is_in_service_status(row[status_col]):
                    for side in ('A', 'Z'):
                        loc_col = f'{side}-LOC:CAB:RU'
                        if loc_col in row.index:
                            loc = str(row[loc_col]).strip()
                            if loc and loc != 'nan':
                                in_service_locs.add(loc)

    # Second pass: bucket devices by location
    for sheet_name, df in roce_df_dict.items():
        # A-side devices
        a_side = df[['A-LOC:CAB:RU', 'A-MODEL']].dropna(subset=['A-LOC:CAB:RU', 'A-MODEL'])
        for _, row in a_side.iterrows():
            loc = sanitize_cell_value(str(row['A-LOC:CAB:RU']))
            model = sanitize_cell_value(str(row['A-MODEL']))
            if loc and loc not in seen_locations:
                seen_locations.add(loc)
                target = in_service_list if (not sort_by_status or loc in in_service_locs) else not_in_service_list
                put_optic_in_list(target, model)

        # Z-side devices (skip if location already counted)
        z_side = df[['Z-LOC:CAB:RU', 'Z-MODEL']].dropna(subset=['Z-LOC:CAB:RU', 'Z-MODEL'])
        for _, row in z_side.iterrows():
            loc = sanitize_cell_value(str(row['Z-LOC:CAB:RU']))
            model = sanitize_cell_value(str(row['Z-MODEL']))
            if loc and loc not in seen_locations:
                seen_locations.add(loc)
                target = in_service_list if (not sort_by_status or loc in in_service_locs) else not_in_service_list
                put_optic_in_list(target, model)

    return in_service_list, not_in_service_list


##### Logic for counting unique devices in a cutsheet based on location ID in column C (A-LOC:CAB:RU / Z-LOC:CAB:RU)
##### and model in column E (A-MODEL / Z-MODEL). Rows sharing the same location ID are the same physical device.
# When sort_by_status=True, a device location is In Service if any row with that
# location has a verified/completed status.
# When sort_by_status=False, all devices go into in_service and not_in_service is empty.
def count_devices_cutsheet(input_file, sort_by_status=False):
    cutsheet, _sheet_name = _read_cutsheet_df(input_file)
    if cutsheet is None:
        return [], []
    seen_locations = set()
    in_service_locs = set()
    in_service_list = []
    not_in_service_list = []

    # First pass: find which locations have at least one in-service row
    if sort_by_status:
        status_col = None
        for col in cutsheet.columns:
            if str(col).lower() == 'status':
                status_col = col
                break
        if status_col:
            for _, row in cutsheet.iterrows():
                if _is_in_service_status(row[status_col]):
                    for side in ('A', 'Z'):
                        loc_col = f'{side}-LOC:CAB:RU'
                        if loc_col in cutsheet.columns:
                            loc = str(row[loc_col]).strip()
                            if loc and loc != 'nan':
                                in_service_locs.add(loc)

    # Second pass: bucket devices by location
    # A-side devices
    a_cols = ['A-LOC:CAB:RU', 'A-MODEL']
    if all(c in cutsheet.columns for c in a_cols):
        a_side = cutsheet[a_cols].dropna(subset=a_cols)
        for _, row in a_side.iterrows():
            loc = sanitize_cell_value(str(row['A-LOC:CAB:RU']))
            model = sanitize_cell_value(str(row['A-MODEL']))
            if loc and loc not in seen_locations:
                seen_locations.add(loc)
                target = in_service_list if (not sort_by_status or loc in in_service_locs) else not_in_service_list
                put_optic_in_list(target, model)

    # Z-side devices (skip if location already counted from A-side)
    z_cols = ['Z-LOC:CAB:RU', 'Z-MODEL']
    if all(c in cutsheet.columns for c in z_cols):
        z_side = cutsheet[z_cols].dropna(subset=z_cols)
        for _, row in z_side.iterrows():
            loc = sanitize_cell_value(str(row['Z-LOC:CAB:RU']))
            model = sanitize_cell_value(str(row['Z-MODEL']))
            if loc and loc not in seen_locations:
                seen_locations.add(loc)
                target = in_service_list if (not sort_by_status or loc in in_service_locs) else not_in_service_list
                put_optic_in_list(target, model)

    # -- SITE-HOSTS pass ----------------------------------------------------
    # Devices that exist in SITE-HOSTS but have no cable connections in the
    # CUTSHEET (e.g. compute nodes staged but not yet cabled) are invisible to
    # the location-based pass above.  We pick them up here as a second source,
    # skipping any hostname already represented by a CUTSHEET connection row to
    # avoid double-counting.  Empty or non-in-service STATUS → Not In Service.
    site_hosts = _read_site_hosts_tab(input_file)
    if site_hosts is not None:
        # Collect device hostnames already covered by CUTSHEET connections.
        seen_hostnames_lower: set = set()
        for _dcol in _CUTSHEET_A_DEVICE_COLS + _CUTSHEET_Z_DEVICE_COLS:
            if _dcol in cutsheet.columns:
                for v in cutsheet[_dcol].dropna():
                    h = sanitize_cell_value(str(v))
                    if h and h.casefold() != 'nan':
                        seen_hostnames_lower.add(h.casefold())

        h_col = _first_col(site_hosts, _SITE_HOSTS_HOSTNAME_COLS)
        m_col = _first_col(site_hosts, _SITE_HOSTS_MODEL_COLS)
        s_col = _first_col(site_hosts, _SITE_HOSTS_STATUS_COLS)

        if h_col and m_col:
            seen_sh_lower: set = set()
            for row in site_hosts.to_dict('records'):
                hostname = sanitize_cell_value(str(row.get(h_col, '')))
                model    = sanitize_cell_value(str(row.get(m_col, '')))
                if not hostname or hostname.casefold() == 'nan':
                    continue
                if not model or model.casefold() == 'nan':
                    continue
                hkey = hostname.casefold()
                # Skip: already covered by a CUTSHEET connection row
                if hkey in seen_hostnames_lower:
                    continue
                # Skip: already counted from a previous SITE-HOSTS row
                if hkey in seen_sh_lower:
                    continue
                seen_sh_lower.add(hkey)
                # Determine bucket from SITE-HOSTS STATUS
                raw_status = row.get(s_col, '') if s_col else ''
                is_svc = _is_in_service_status(raw_status) if raw_status else False
                if sort_by_status:
                    target = in_service_list if is_svc else not_in_service_list
                else:
                    target = in_service_list
                put_optic_in_list(target, model)

    return in_service_list, not_in_service_list


def count_cutsheet_by_all_statuses(input_file):
    """Count optics grouped by every unique STATUS value in the cutsheet.
    Returns { status_value: [OpticType] }.
    Breakout deduplication is shared across all status groups.
    """
    cutsheet, _sheet_name = _read_cutsheet_df(input_file)
    if cutsheet is None:
        return {}

    status_col = None
    for col in cutsheet.columns:
        if str(col).upper() == 'STATUS':
            status_col = col
            break

    if status_col is None:
        return {"No STATUS column found": []}

    breakout_ports = []
    status_optics = {}

    for _, row in cutsheet.iterrows():
        raw = str(row[status_col])
        status = " ".join(raw.split()) if raw != 'nan' else "No Status"
        if status not in status_optics:
            status_optics[status] = []
        process_cutsheet_row(row, breakout_ports, status_optics[status], 'A')
        process_cutsheet_row(row, breakout_ports, status_optics[status], 'Z')

    return status_optics


def count_devices_cutsheet_by_status(input_file):
    """Categorize each unique device location into Fully Cabled, Work in Progress, or Not Cabled.

    Fully Cabled     — every row for this device has STATUS == 'LLDP: Passed'
    Not Cabled       — every row for this device has STATUS starting with 'Cable Not Run'
    Work in Progress — any mix of statuses

    Returns { category: [OpticType] } where each OpticType represents a device model.
    """
    cutsheet, _sheet_name = _read_cutsheet_df(input_file)
    if cutsheet is None:
        return {}

    status_col = None
    for col in cutsheet.columns:
        if str(col).upper() == 'STATUS':
            status_col = col
            break

    if status_col is None:
        return {"No STATUS column found": []}

    location_statuses = {}  # { loc_id: set(normalized_statuses) }
    location_model = {}     # { loc_id: model }

    for _, row in cutsheet.iterrows():
        raw = str(row[status_col])
        status = " ".join(raw.split()) if raw != 'nan' else "No Status"

        for side in ('A', 'Z'):
            loc_col = f'{side}-LOC:CAB:RU'
            model_col = f'{side}-MODEL'
            if loc_col not in cutsheet.columns:
                continue
            loc = sanitize_cell_value(str(row[loc_col]).strip())
            if not loc or loc == 'nan':
                continue
            if loc not in location_statuses:
                location_statuses[loc] = set()
                if model_col in cutsheet.columns:
                    model = sanitize_cell_value(str(row.get(model_col, '')).strip())
                    if model and model != 'nan':
                        location_model[loc] = model
            location_statuses[loc].add(status)

    categories = {"Fully Cabled": [], "Work in Progress": [], "Not Cabled": []}

    for loc, statuses in location_statuses.items():
        model = location_model.get(loc, "Unknown")
        if all(_is_lldp_passed(s) for s in statuses):
            category = "Fully Cabled"
        elif all(s.startswith("Cable Not Run") for s in statuses):
            category = "Not Cabled"
        else:
            category = "Work in Progress"
        put_optic_in_list(categories[category], model)

    return categories


def create_device_status_breakdown_string(device_categories, print_out):
    print_out += "--- Device Status ---\n\n"
    total_devices = sum(sum(o.count for o in items) for items in device_categories.values())

    for category in ("Fully Cabled", "Work in Progress", "Not Cabled"):
        items = device_categories.get(category, [])
        cat_total = sum(o.count for o in items)
        pct = (cat_total / total_devices * 100) if total_devices else 0
        print_out += f"{category}  ({cat_total} devices, {pct:.1f}%)\n"
        for item in sorted(items, key=lambda x: x.count, reverse=True):
            print_out += f"  {item.string_count()}\n"
        print_out += "\n"

    print_out += f"Total: {total_devices} devices\n\n"
    return print_out


def create_status_breakdown_string(status_optics, file, print_out):
    print_out += f"{os.path.basename(file)} - Build Status Report\n"
    print_out += "=" * TABLE_WIDTH + "\n\n"

    total_all = sum(sum(o.count for o in optics) for optics in status_optics.values())

    for status in sorted(status_optics.keys()):
        optics = status_optics[status]
        if not optics:
            continue
        status_total = sum(o.count for o in optics)
        pct = (status_total / total_all * 100) if total_all else 0
        print_out += f"{status}  ({status_total} optics, {pct:.1f}%)\n"
        for optic in sorted(optics, key=lambda x: x.count, reverse=True):
            print_out += f"  {optic.string_count()}\n"
        print_out += "\n"

    print_out += f"Total: {total_all} optics across {len(status_optics)} statuses\n\n"
    return print_out


def _ib_row_status(row, df):
    """Return the normalized Status value for a row in an IB sheet."""
    status_col = next((c for c in df.columns if str(c).lower() == 'status'), None)
    raw = str(row[status_col]) if status_col else "No Status"
    return " ".join(raw.split()) if raw != 'nan' else "No Status"


def count_ib_by_all_statuses(input_file):
    """Count optics grouped by every unique Status value in IB sheets.

    Matches count_infini_band logic:
    - Pull schedule sheets with Optic Type: one optic per row grouped by Status
    - Node sheets: IBP rows counted as 'IB Node Optic' x2 per row (both sides of link)
    - UFM sheets: 'Twin Port OSFP' occurrences per row grouped by Status
    """
    xls = _cached_excel_file(input_file)
    active_sheets = _active_sheet_names(xls)
    infini_band = {s: _cached_read_sheet(xls, sheet_name=s) for s in active_sheets}
    status_optics = {}
    sheets_with_optic = []
    node_sheets = []
    ufm_sheets = []

    for tab_name, sheet_data in infini_band.items():
        if "Optic Type" in sheet_data:
            sheets_with_optic.append(tab_name)
        if "node" in tab_name.casefold():
            node_sheets.append(tab_name)
        if "ufm" in tab_name.casefold():
            ufm_sheets.append(tab_name)

    def _add(status, optic_name):
        status_optics.setdefault(status, [])
        put_optic_in_list(status_optics[status], optic_name)

    # Pull schedule sheets with Optic Type
    for active_sheet in sheets_with_optic:
        df = _cached_read_sheet(input_file, sheet_name=active_sheet)
        for _, row in df.iterrows():
            if "Optic Type" in row.index and pd.notna(row["Optic Type"]):
                _add(_ib_row_status(row, df), str(row["Optic Type"]))

    # Node sheets — IBP rows x2 (both sides of each link)
    for active_sheet in node_sheets:
        df = _cached_read_sheet(input_file, sheet_name=active_sheet)
        for _, row in df.iterrows():
            if "IBP" in row.index and pd.notna(row["IBP"]):
                status = _ib_row_status(row, df)
                _add(status, "IB Node Optic")
                _add(status, "IB Node Optic")

    # UFM sheets — count Twin Port OSFP occurrences per row
    for active_sheet in ufm_sheets:
        df = _cached_read_sheet(input_file, sheet_name=active_sheet)
        for _, row in df.iterrows():
            twin_count = (row == "Twin Port OSFP").sum()
            if twin_count > 0:
                status = _ib_row_status(row, df)
                for _ in range(twin_count):
                    _add(status, "Twin Port OSFP")

    return status_optics


def count_devices_ib_by_status(input_file):
    """Categorize each unique IB device into Fully Cabled, Work in Progress, or Not Cabled.

    Fully Cabled     — every row for this device has Status == 'Cable Is Ran: Complete'
    Not Cabled       — every row for this device has Status == 'Cable Not Run'
    Work in Progress — any mix (includes Blocked, Not Terminated, or mixed)
    """
    xls = _cached_excel_file(input_file)
    device_statuses = {}
    _JUNK_WORDS = {"status", "source", "destination", "n/a", "nan"}

    for sheet_name in xls.sheet_names:
        if "pull schedule" not in sheet_name.casefold():
            continue

        df = _cached_read_sheet(xls, sheet_name=sheet_name)
        df.columns = [str(c).strip() for c in df.columns]

        if "Source" not in df.columns or "Destination" not in df.columns:
            df = _cached_read_sheet(xls, sheet_name=sheet_name, header=1)
            df.columns = [str(c).strip() for c in df.columns]

        if "Source" not in df.columns or "Destination" not in df.columns:
            continue

        status_col = next((c for c in df.columns if c.lower() == 'status'), None)

        for _, row in df.iterrows():
            raw = str(row[status_col]) if status_col else "No Status"
            status = " ".join(raw.split()) if raw != 'nan' else "No Status"

            for col in ("Source", "Destination"):
                val = str(row[col]).strip() if pd.notna(row[col]) else ""
                if val and " " not in val and val.lower() not in _JUNK_WORDS:
                    device_statuses.setdefault(val, set()).add(status)

    categories = {"Fully Cabled": [], "Work in Progress": [], "Not Cabled": []}
    for device, statuses in device_statuses.items():
        if all(s == "Cable Is Ran: Complete" for s in statuses):
            category = "Fully Cabled"
        elif all(s == "Cable Not Run" for s in statuses):
            category = "Not Cabled"
        else:
            category = "Work in Progress"
        put_optic_in_list(categories[category], "IB Switch")

    return categories


def _normalize_roce_status(raw):
    """Normalize a raw RoCE status cell value."""
    val = str(raw).strip()
    if not val or val == 'nan':
        return "Unknown"
    if 'complete' in val.lower():
        return "Complete"
    return val


def count_roce_by_all_statuses(input_file):
    """Count optics grouped by normalized Status value in RoCE sheets.

    Reuses the same port deduplication logic as count_roce, including
    tier-0 to tier-1 port stripping. Blank status → Unknown,
    any 'complete' variant → Complete.
    """
    xls = _cached_excel_file(input_file)
    active_sheets = _active_sheet_names(xls)
    roce_df_dict = {s: _cached_read_sheet(xls, sheet_name=s) for s in active_sheets}
    status_optics = {}
    a_string = 'A'
    z_string = 'Z'

    def _add(status, optic_name):
        status_optics.setdefault(status, [])
        put_optic_in_list(status_optics[status], optic_name)

    def _process_row(row, occupied_ports, status, port, side):
        if str(row[side + '-SIDE-DNS-NAME']) != 'nan' and str(row[side + '-OPTIC']) != 'nan':
            if not check_if_roce_port_occupied(row[side + '-LOC:CAB:RU'], port, row[side + '-CONNECTOR'], occupied_ports):
                _add(status, str(row[side + '-OPTIC']))
                occupied_ports.append(str(row[side + '-LOC:CAB:RU']) + port + str(row[side + '-CONNECTOR']))

    for sheet_name, roce_df in roce_df_dict.items():
        if "backup" in sheet_name.lower():
            continue

        status_col = next((c for c in roce_df.columns if str(c).lower() == 'status'), None)
        occupied_ports = []

        for _, row in roce_df.iterrows():
            raw = row[status_col] if status_col else ''
            status = _normalize_roce_status(raw)

            if "node to tier-0" in sheet_name.lower():
                _process_row(row, occupied_ports, status, str(row[a_string + '-PORT']), a_string)
                _process_row(row, occupied_ports, status, str(row[z_string + '-PORT']), z_string)
            elif "tier-0 to tier-1" in sheet_name.lower():
                _process_row(row, occupied_ports, status, str(row[a_string + '-PORT'])[:-2], a_string)
                _process_row(row, occupied_ports, status, str(row[z_string + '-PORT'])[:-2], z_string)

    return status_optics


def count_devices_roce_by_status(input_file):
    """Categorize each unique RoCE device location into Fully Cabled, Work in Progress, or Not Cabled.

    Fully Cabled     — every row for this location has a Complete status
    Not Cabled       — every row is 'No Label & Not Yet Run' or 'Unknown' (blank)
    Work in Progress — any mix
    """
    xls = _cached_excel_file(input_file)
    active_sheets = _active_sheet_names(xls)
    roce_df_dict = {s: _cached_read_sheet(xls, sheet_name=s) for s in active_sheets}
    seen_locations = set()
    location_statuses = {}
    location_model = {}
    _not_cabled = {"No Label & Not Yet Run", "Unknown"}

    for sheet_name, df in roce_df_dict.items():
        status_col = next((c for c in df.columns if str(c).lower() == 'status'), None)

        for _, row in df.iterrows():
            raw = row[status_col] if status_col else ''
            status = _normalize_roce_status(raw)

            for side in ('A', 'Z'):
                loc_col = f'{side}-LOC:CAB:RU'
                model_col = f'{side}-MODEL'
                if loc_col not in df.columns:
                    continue
                loc = sanitize_cell_value(str(row[loc_col]).strip())
                if not loc or loc == 'nan':
                    continue
                location_statuses.setdefault(loc, set()).add(status)
                if loc not in location_model and model_col in df.columns:
                    model = sanitize_cell_value(str(row.get(model_col, '')).strip())
                    if model and model != 'nan':
                        location_model[loc] = model

    categories = {"Fully Cabled": [], "Work in Progress": [], "Not Cabled": []}
    for loc, statuses in location_statuses.items():
        if loc in seen_locations:
            continue
        seen_locations.add(loc)
        model = location_model.get(loc, "Unknown")
        if all(s == "Complete" for s in statuses):
            category = "Fully Cabled"
        elif all(s in _not_cabled for s in statuses):
            category = "Not Cabled"
        else:
            category = "Work in Progress"
        put_optic_in_list(categories[category], model)

    return categories


def count_all_files_build_status_gui(files_to_count):
    final_to_print = ""
    for file in files_to_count:
        file_type = get_file_type(file)
        if file_type == file_type_for_cutsheet:
            status_optics = count_cutsheet_by_all_statuses(file)
            final_to_print = create_status_breakdown_string(status_optics, file, final_to_print)
            device_categories = count_devices_cutsheet_by_status(file)
            final_to_print = create_device_status_breakdown_string(device_categories, final_to_print)
        elif file_type == file_type_for_ib:
            status_optics = count_ib_by_all_statuses(file)
            final_to_print = create_status_breakdown_string(status_optics, file, final_to_print)
            device_categories = count_devices_ib_by_status(file)
            final_to_print = create_device_status_breakdown_string(device_categories, final_to_print)
        elif file_type == file_type_for_roce:
            status_optics = count_roce_by_all_statuses(file)
            final_to_print = create_status_breakdown_string(status_optics, file, final_to_print)
            device_categories = count_devices_roce_by_status(file)
            final_to_print = create_device_status_breakdown_string(device_categories, final_to_print)
        else:
            final_to_print += f"{os.path.basename(file)}: Build Status Report supports cutsheet, IB, and RoCE files only.\n\n"
    return final_to_print


def count_all_files_gui(files_to_count):
    final_to_print = ""

    for file in files_to_count:
        file_type = get_file_type(file)
        if file_type == file_type_for_cutsheet:
            current_count, _ = count_cutsheet(file)
            final_to_print = create_count_string(current_count, file, final_to_print, "Optic Count")
            device_count, _ = count_devices_cutsheet(file)
            final_to_print = create_count_string(device_count, file, final_to_print, "Device Count")
        elif file_type == file_type_for_ib:
            current_count, _ = count_infini_band(file)
            final_to_print = create_count_string(current_count, file, final_to_print, "Optic Count")
            device_count, _ = count_devices_infini_band(file)
            final_to_print = create_count_string(device_count, file, final_to_print, "Device Count")
        elif file_type == file_type_for_roce:
            current_count, _ = count_roce(file)
            final_to_print = create_count_string(current_count, file, final_to_print, "Optic Count")
            device_count, _ = count_devices_roce(file)
            final_to_print = create_count_string(device_count, file, final_to_print, "Device Count")
        else:
            final_to_print += f"Unsupported file type: {os.path.basename(file)}\n"
        final_to_print += "\n"

    return final_to_print

def count_all_files_gui_by_status(files_to_count):
    final_to_print = ""

    for file in files_to_count:
        file_type = get_file_type(file)
        if file_type == file_type_for_cutsheet:
            in_service, not_in_service = count_cutsheet(file, sort_by_status=True)
            final_to_print = create_side_by_side_string(in_service, not_in_service, file, final_to_print, "Optic Count")
            dev_in, dev_not = count_devices_cutsheet(file, sort_by_status=True)
            final_to_print = create_side_by_side_string(dev_in, dev_not, file, final_to_print, "Device Count")
        elif file_type == file_type_for_ib:
            in_service, not_in_service = count_infini_band(file, sort_by_status=True)
            final_to_print = create_side_by_side_string(in_service, not_in_service, file, final_to_print, "Optic Count")
            dev_in, dev_not = count_devices_infini_band(file, sort_by_status=True)
            final_to_print = create_side_by_side_string(dev_in, dev_not, file, final_to_print, "Device Count")
        elif file_type == file_type_for_roce:
            in_service, not_in_service = count_roce(file, sort_by_status=True)
            final_to_print = create_side_by_side_string(in_service, not_in_service, file, final_to_print, "Optic Count")
            dev_in, dev_not = count_devices_roce(file, sort_by_status=True)
            final_to_print = create_side_by_side_string(dev_in, dev_not, file, final_to_print, "Device Count")
        else:
            final_to_print += f"Unsupported file type: {os.path.basename(file)}\n"
        final_to_print += "\n"

    return final_to_print

def create_count_string(items, file, print_out, label):
    title = f"{os.path.basename(file)} {label}"
    print_out += f"{title:^{TABLE_WIDTH}}\n"
    for item in items:
        print_out += f"{item.string_count()}\n"
    total = sum(item.count for item in items)
    print_out += "-" * TABLE_WIDTH + "\n"
    print_out += f"Total: {total}\n"
    print_out += "\n"
    return print_out

def create_side_by_side_string(in_service, not_in_service, file, print_out, label):
    separator = "-" * COL_WIDTH + "-+-" + "-" * COL_WIDTH
    print_out += f"{os.path.basename(file):^{TABLE_WIDTH}}\n"
    print_out += f"{f'{label} - In Service':<{COL_WIDTH}} | {f'{label} - Not In Service':<{COL_WIDTH}}\n"
    print_out += separator + "\n"

    # Build ordered list of all unique names across both sides
    seen = set()
    all_names = []
    for item in in_service + not_in_service:
        if item.name not in seen:
            all_names.append(item.name)
            seen.add(item.name)

    in_lookup  = {o.name: o.count for o in in_service}
    not_lookup = {o.name: o.count for o in not_in_service}

    # Sort by total count descending — highest volume optic/device at the top
    all_names.sort(key=lambda n: in_lookup.get(n, 0) + not_lookup.get(n, 0), reverse=True)

    max_name_len = COL_WIDTH - 8  # reserve space for ": 99999"
    count_width  = COL_WIDTH - max_name_len - 2  # right-align counts at a fixed column
    for name in all_names:
        display_name = name if len(name) <= max_name_len else name[:max_name_len - 1] + "…"
        left  = f"{display_name:<{max_name_len}}: {in_lookup.get(name, 0):>{count_width}}"
        right = f"{display_name:<{max_name_len}}: {not_lookup.get(name, 0):>{count_width}}"
        print_out += f"{left:<{COL_WIDTH}} | {right:<{COL_WIDTH}}\n"

    total_in  = sum(o.count for o in in_service)
    total_not = sum(o.count for o in not_in_service)
    print_out += separator + "\n"
    left_total  = f"{'Total':<{max_name_len}}: {total_in:>{count_width}}"
    right_total = f"{'Total':<{max_name_len}}: {total_not:>{count_width}}"
    print_out += f"{left_total:<{COL_WIDTH}} | {right_total:<{COL_WIDTH}}\n"
    print_out += separator + "\n"
    grand_total = total_in + total_not
    print_out += f"{'Total (In + Not In Service): ' + str(grand_total):^{TABLE_WIDTH}}\n"
    print_out += "\n"
    return print_out


def _extract_cutsheet_column_c_locations(input_file):
    result = {
        "source_sheet": None,
        "location_c_rows": [],
        "location_c_index": {},
        "warnings": [],
    }

    cutsheet, sheet_name = _read_cutsheet_df(input_file)
    if cutsheet is None:
        result["warnings"].append("cutsheet_tab_not_found")
        return result

    result["source_sheet"] = sheet_name
    location_col = "A-LOC:CAB:RU"
    optic_cols = ("A-OPTIC", "Z-OPTIC")

    if location_col not in cutsheet.columns:
        result["warnings"].append("column_c_header_not_found:A-LOC:CAB:RU")
        return result

    for idx, row in cutsheet.iterrows():
        row_number = int(idx) + 2  # +2 to align with Excel row numbers (header row + 1-indexed).
        location = _normalize_cell(row.get(location_col))
        if not location:
            continue

        optics = []
        for optic_col in optic_cols:
            if optic_col not in cutsheet.columns:
                continue
            optic = _normalize_cell(row.get(optic_col))
            if optic:
                optics.append((optic_col, optic))

        if not optics:
            continue

        for optic_col, optic in optics:
            row_record = {
                "row_number": row_number,
                "location_c": location,
                "optic": optic,
                "optic_column": optic_col,
            }
            result["location_c_rows"].append(row_record)

            location_entry = result["location_c_index"].setdefault(
                location,
                {"optics": {}},
            )
            optic_entry = location_entry["optics"].setdefault(
                optic,
                {"count": 0, "rows": []},
            )
            optic_entry["count"] += 1
            if row_number not in optic_entry["rows"]:
                optic_entry["rows"].append(row_number)

    return result


def _extract_device_models(cutsheet_df):
    """
    Build a device model index from the cutsheet DataFrame.
    Tracks model counts and which locations each model appears in.
    Returns:
        model_index: {model: {count, a_side_count, z_side_count, locations: {loc: count}}}
        location_model_index: {location: {models: {model: count}}}
    """
    result = {
        "model_index": {},
        "location_model_index": {},
        "warnings": [],
    }

    side_pairs = [
        ("A-MODEL", "A-LOC:CAB:RU", "a_side_count"),
        ("Z-MODEL", "Z-LOC:CAB:RU", "z_side_count"),
    ]

    for model_col, loc_col, side_key in side_pairs:
        if model_col not in cutsheet_df.columns:
            result["warnings"].append(f"column_not_found:{model_col}")
            continue

        for _, row in cutsheet_df.iterrows():
            model = _normalize_cell(row.get(model_col))
            if not model:
                continue
            # Normalize case so "sn3700" and "SN3700" merge into one entry
            model = model.upper()

            location = _normalize_cell(row.get(loc_col)) if loc_col in cutsheet_df.columns else None

            # model_index entry
            entry = result["model_index"].setdefault(
                model, {"count": 0, "a_side_count": 0, "z_side_count": 0, "locations": {}}
            )
            entry["count"] += 1
            entry[side_key] += 1
            if location:
                entry["locations"][location] = entry["locations"].get(location, 0) + 1

            # location_model_index entry
            if location:
                loc_entry = result["location_model_index"].setdefault(location, {"models": {}})
                loc_entry["models"][model] = loc_entry["models"].get(model, 0) + 1

    return result


def count_file(input_file):
    file_type = get_file_type(input_file)
    if file_type == file_type_for_cutsheet:
        optics, _ = count_cutsheet(input_file)
        return optics, file_type
    if file_type == file_type_for_ib:
        optics, _ = count_infini_band(input_file)
        return optics, file_type
    if file_type == file_type_for_roce:
        optics, _ = count_roce(input_file)
        return optics, file_type
    return [], "unsupported"


def optic_list_to_dict(optics_in):
    result = {}
    for optic in optics_in:
        result[optic.name] = optic.count
    return result


def build_sheet_context(files_to_count):
    context = {
        "files": [],
        "summary": {},
        "device_model_summary": {},
        "cutsheet_location_c_rows": [],
        "cutsheet_location_c_index": {},
        "cutsheet_device_model_index": {},
        "parser_warnings": [],
    }
    aggregate = {}
    aggregate_models = {}

    for file_path in files_to_count:
        optics, file_type = count_file(file_path)
        counts = optic_list_to_dict(optics)
        file_context = {
            "file_name": os.path.basename(file_path),
            "file_path": file_path,
            "source_type": file_type,
            "counts": counts,
        }

        if file_type == file_type_for_cutsheet:
            location_data = _extract_cutsheet_column_c_locations(file_path)
            file_context["location_c_rows"] = location_data["location_c_rows"]
            file_context["location_c_index"] = location_data["location_c_index"]
            file_context["location_source_sheet"] = location_data["source_sheet"]
            if location_data["warnings"]:
                file_context["parser_warnings"] = location_data["warnings"]
                for warning in location_data["warnings"]:
                    context["parser_warnings"].append(
                        {"file_name": os.path.basename(file_path), "warning": warning}
                    )

            for row_record in location_data["location_c_rows"]:
                global_row = dict(row_record)
                global_row["file_name"] = os.path.basename(file_path)
                context["cutsheet_location_c_rows"].append(global_row)

                location = row_record["location_c"]
                optic = row_record["optic"]
                location_entry = context["cutsheet_location_c_index"].setdefault(
                    location, {"optics": {}}
                )
                optic_entry = location_entry["optics"].setdefault(
                    optic, {"count": 0, "evidence": []}
                )
                optic_entry["count"] += 1
                optic_entry["evidence"].append(
                    {
                        "file_name": os.path.basename(file_path),
                        "row_number": row_record["row_number"],
                        "optic_column": row_record["optic_column"],
                    }
                )

            # Device model extraction
            cutsheet_df, _ = _read_cutsheet_df(file_path)
            if cutsheet_df is not None:
                model_data = _extract_device_models(cutsheet_df)
                file_context["device_model_index"] = model_data["model_index"]
                file_context["device_model_warnings"] = model_data["warnings"]

                for model, model_info in model_data["model_index"].items():
                    if model not in aggregate_models:
                        aggregate_models[model] = {
                            "count": 0, "a_side_count": 0, "z_side_count": 0, "locations": {}
                        }
                    aggregate_models[model]["count"] += model_info["count"]
                    aggregate_models[model]["a_side_count"] += model_info["a_side_count"]
                    aggregate_models[model]["z_side_count"] += model_info["z_side_count"]
                    for loc, loc_count in model_info["locations"].items():
                        aggregate_models[model]["locations"][loc] = (
                            aggregate_models[model]["locations"].get(loc, 0) + loc_count
                        )

                for loc, loc_info in model_data["location_model_index"].items():
                    existing = context["cutsheet_device_model_index"].setdefault(loc, {"models": {}})
                    for model, cnt in loc_info["models"].items():
                        existing["models"][model] = existing["models"].get(model, 0) + cnt

        for optic_name, qty in counts.items():
            aggregate[optic_name] = aggregate.get(optic_name, 0) + qty
        context["files"].append(file_context)

    context["summary"] = aggregate
    context["device_model_summary"] = aggregate_models
    return context


def count_and_build_context(files_to_count):
    """Single-pass equivalent of count_all_files_gui + build_sheet_context.

    Returns (count_text, context_dict).  Each file is counted once; DataFrames
    stay cached in _cached_read_sheet across all sub-calls, so no sheet is
    parsed from disk more than once per request.
    """
    final_to_print = ""
    context = {
        "files": [],
        "summary": {},
        "device_model_summary": {},
        "cutsheet_location_c_rows": [],
        "cutsheet_location_c_index": {},
        "cutsheet_device_model_index": {},
        "parser_warnings": [],
    }
    aggregate = {}
    aggregate_models = {}

    for file_path in files_to_count:
        file_type = get_file_type(file_path)
        optics = []

        if file_type == file_type_for_cutsheet:
            optics, _ = count_cutsheet(file_path)
            final_to_print = create_count_string(optics, file_path, final_to_print, "Optic Count")
            devices, _ = count_devices_cutsheet(file_path)
            final_to_print = create_count_string(devices, file_path, final_to_print, "Device Count")
        elif file_type == file_type_for_ib:
            optics, _ = count_infini_band(file_path)
            final_to_print = create_count_string(optics, file_path, final_to_print, "Optic Count")
            devices, _ = count_devices_infini_band(file_path)
            final_to_print = create_count_string(devices, file_path, final_to_print, "Device Count")
        elif file_type == file_type_for_roce:
            optics, _ = count_roce(file_path)
            final_to_print = create_count_string(optics, file_path, final_to_print, "Optic Count")
            devices, _ = count_devices_roce(file_path)
            final_to_print = create_count_string(devices, file_path, final_to_print, "Device Count")
        else:
            final_to_print += f"Unsupported file type: {os.path.basename(file_path)}\n"
        final_to_print += "\n"

        counts = optic_list_to_dict(optics)
        file_context = {
            "file_name": os.path.basename(file_path),
            "file_path": file_path,
            "source_type": file_type,
            "counts": counts,
        }

        if file_type == file_type_for_cutsheet:
            location_data = _extract_cutsheet_column_c_locations(file_path)
            file_context["location_c_rows"] = location_data["location_c_rows"]
            file_context["location_c_index"] = location_data["location_c_index"]
            file_context["location_source_sheet"] = location_data["source_sheet"]
            if location_data["warnings"]:
                file_context["parser_warnings"] = location_data["warnings"]
                for warning in location_data["warnings"]:
                    context["parser_warnings"].append(
                        {"file_name": os.path.basename(file_path), "warning": warning}
                    )

            for row_record in location_data["location_c_rows"]:
                global_row = dict(row_record)
                global_row["file_name"] = os.path.basename(file_path)
                context["cutsheet_location_c_rows"].append(global_row)

                location = row_record["location_c"]
                optic = row_record["optic"]
                location_entry = context["cutsheet_location_c_index"].setdefault(
                    location, {"optics": {}}
                )
                optic_entry = location_entry["optics"].setdefault(
                    optic, {"count": 0, "evidence": []}
                )
                optic_entry["count"] += 1
                optic_entry["evidence"].append({
                    "file_name": os.path.basename(file_path),
                    "row_number": row_record["row_number"],
                    "optic_column": row_record["optic_column"],
                })

            cutsheet_df, _ = _read_cutsheet_df(file_path)
            if cutsheet_df is not None:
                model_data = _extract_device_models(cutsheet_df)
                file_context["device_model_index"] = model_data["model_index"]
                file_context["device_model_warnings"] = model_data["warnings"]

                for model, model_info in model_data["model_index"].items():
                    if model not in aggregate_models:
                        aggregate_models[model] = {
                            "count": 0, "a_side_count": 0, "z_side_count": 0, "locations": {}
                        }
                    aggregate_models[model]["count"] += model_info["count"]
                    aggregate_models[model]["a_side_count"] += model_info["a_side_count"]
                    aggregate_models[model]["z_side_count"] += model_info["z_side_count"]
                    for loc, loc_count in model_info["locations"].items():
                        aggregate_models[model]["locations"][loc] = (
                            aggregate_models[model]["locations"].get(loc, 0) + loc_count
                        )

                for loc, loc_info in model_data["location_model_index"].items():
                    existing = context["cutsheet_device_model_index"].setdefault(loc, {"models": {}})
                    for model, cnt in loc_info["models"].items():
                        existing["models"][model] = existing["models"].get(model, 0) + cnt

        for optic_name, qty in counts.items():
            aggregate[optic_name] = aggregate.get(optic_name, 0) + qty
        context["files"].append(file_context)

    context["summary"] = aggregate
    context["device_model_summary"] = aggregate_models
    return final_to_print, context
