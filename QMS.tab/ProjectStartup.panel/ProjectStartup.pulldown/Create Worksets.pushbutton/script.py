# -*- coding: utf-8 -*-
import sys
import os
import zipfile
import xml.etree.ElementTree as ET
import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

from pyrevit import revit, DB, forms

doc = revit.doc

# Ensure the project is Workshared
if not doc.IsWorkshared:
    forms.alert(
        "The current Revit model is not workshared.\n"
        "Please enable Worksharing before running this tool.",
        exitscript=True
    )

DEFAULT_TEMPLATE_PATH = r"C:\SP\QMS\QMS DO - Revit\Worksets Template"


# --- NATIVE OpenXML EXCEL PARSER ---
def parse_xlsx_column_a(file_path):
    """ Reads Column A directly via OpenXML without requiring openpyxl or Excel COM. """
    names = []
    try:
        with zipfile.ZipFile(file_path, 'r') as z:
            shared_strings = []
            if 'xl/sharedStrings.xml' in z.namelist():
                xml_content = z.read('xl/sharedStrings.xml')
                tree = ET.fromstring(xml_content)
                ns = {'main': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                for si in tree.findall('main:si', ns):
                    t_elems = si.findall('.//main:t', ns)
                    text = "".join([t.text for t in t_elems if t.text is not None])
                    shared_strings.append(text)

            if 'xl/worksheets/sheet1.xml' in z.namelist():
                xml_content = z.read('xl/worksheets/sheet1.xml')
                tree = ET.fromstring(xml_content)
                ns = {'main': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                
                for cell in tree.findall('.//main:c', ns):
                    r_attr = cell.attrib.get('r', '')
                    if r_attr.startswith('A'):  # Column A target
                        cell_type = cell.attrib.get('t', '')
                        v_elem = cell.find('main:v', ns)
                        if v_elem is not None and v_elem.text is not None:
                            val_str = v_elem.text
                            if cell_type == 's' and shared_strings:
                                idx = int(val_str)
                                if idx < len(shared_strings):
                                    val_str = shared_strings[idx]
                            
                            val_str = val_str.strip()
                            if val_str:
                                names.append(val_str)
        return names
    except Exception:
        return []


# --- STEP 1: PROMPT USER TO SELECT EXCEL FILE ---
initial_dir = DEFAULT_TEMPLATE_PATH if os.path.exists(DEFAULT_TEMPLATE_PATH) else None

excel_path = forms.pick_file(
    file_ext='xlsx',
    multi_file=False,
    init_dir=initial_dir,
    title='Select Workset Setup Excel File'
)

if not excel_path:
    sys.exit()

raw_workset_list = parse_xlsx_column_a(excel_path)

if not raw_workset_list:
    forms.alert("No valid workset names found in Column A of the selected file.", exitscript=True)


# --- STEP 2: PARSE & CLEAN EXCEL WORKSET NAMES ---
target_workset_names = []

start_idx = 0
first_val_lower = raw_workset_list[0].lower()
if "workset" in first_val_lower or "name" in first_val_lower or "qms" in first_val_lower:
    start_idx = 1

for name in raw_workset_list[start_idx:]:
    clean_name = name.strip()
    if clean_name and clean_name not in target_workset_names:
        target_workset_names.append(clean_name)

if not target_workset_names:
    forms.alert("No workset names found in the Excel spreadsheet.", exitscript=True)


# --- STEP 3: COLLECT EXISTING WORKSETS IN CURRENT MODEL ---
existing_worksets = DB.FilteredWorksetCollector(doc)\
                      .OfKind(DB.WorksetKind.UserWorkset)\
                      .ToWorksets()

# Case-insensitive set of current workset names
existing_names_lower = {ws.Name.lower() for ws in existing_worksets}


# --- STEP 4: CREATE ONLY NEW (MISSING) WORKSETS ---
created_count = 0
skipped_count = 0
created_workset_list = []

t = DB.Transaction(doc, "Import Worksets from Excel")
t.Start()

for ws_name in target_workset_names:
    if ws_name.lower() in existing_names_lower:
        # Already exists in project: Skip creation
        skipped_count += 1
    else:
        try:
            # Does not exist: Create new workset
            DB.Workset.Create(doc, ws_name)
            created_count += 1
            created_workset_list.append(ws_name)
            # Add to local cache to avoid duplicate creates within loop
            existing_names_lower.add(ws_name.lower())
        except Exception as e:
            print("Failed to create workset '{}': {}".format(ws_name, str(e)))

t.Commit()


# --- STEP 5: DISPLAY SUMMARY REPORT ---
summary_msg = [
    "Workset Import Completed:\n",
    "• Newly Created: {}".format(created_count),
    "• Already Existed (Skipped): {}\n".format(skipped_count)
]

if created_workset_list:
    summary_msg.append("Added Worksets:")
    for w_name in created_workset_list:
        summary_msg.append("  - " + w_name)

forms.alert("\n".join(summary_msg), title="Workset Generator")