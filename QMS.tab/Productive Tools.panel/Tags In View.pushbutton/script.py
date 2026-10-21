# -*- coding: utf-8 -*-
import sys
import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

from pyrevit import revit, DB, forms
from Autodesk.Revit.UI import Selection
from System.Collections.Generic import List

doc = revit.doc
uidoc = revit.uidoc


# --- 1. SELECTION FILTER (RESTRICT ON-SCREEN PICKING TO TAGS ONLY) ---
class TagSelectionFilter(Selection.ISelectionFilter):
    def AllowElement(self, elem):
        if not elem or not elem.Category:
            return False
        
        # Ensure selected element is an independent tag or room/space tag
        if isinstance(elem, DB.IndependentTag) or "Tag" in elem.Category.Name:
            return True
        return False

    def AllowReference(self, reference, point):
        return True


# --- 2. STEP 1: PROMPT USER TO PICK TAGS ON SCREEN FIRST ---
try:
    picked_refs = uidoc.Selection.PickObjects(
        Selection.ObjectType.Element,
        TagSelectionFilter(),
        "Select sample Tags on screen. Click 'Finish' in the top-left options bar when done."
    )
except Exception:
    # User cancelled or hit ESC
    sys.exit()

if not picked_refs:
    sys.exit()


# --- 3. STEP 2: RECOGNIZE UNIQUE CATEGORIES & SYMBOL TYPES ---
target_category_ids = set()
target_type_ids = set()

for ref in picked_refs:
    tag_elem = doc.GetElement(ref.ElementId)
    if not tag_elem:
        continue
    
    # Store Category ID
    if tag_elem.Category:
        target_category_ids.add(tag_elem.Category.Id)
    
    # Store Type ID (FamilySymbol)
    type_id = tag_elem.GetTypeId()
    if type_id and type_id != DB.ElementId.InvalidElementId:
        target_type_ids.add(type_id)

if not target_category_ids:
    forms.alert("No valid tag categories were recognized from your selection.", title="Error")
    sys.exit()


# --- 4. STEP 3: COLLECT ALL MATCHING TAGS IN ACTIVE VIEW ---
# Filter tags visible in the current active view matching picked categories
cat_filter = DB.ElementMulticategoryFilter(List[DB.ElementId](target_category_ids))

view_tags_collector = DB.FilteredElementCollector(doc, doc.ActiveView.Id)\
                        .WherePasses(cat_filter)\
                        .WhereElementIsNotElementType()\
                        .ToElements()

matching_tag_ids = List[DB.ElementId]()

for tag in view_tags_collector:
    # Check if element type matches any of the picked tag types
    tag_type_id = tag.GetTypeId()
    if tag_type_id in target_type_ids:
        matching_tag_ids.Add(tag.Id)


# --- 5. STEP 4: APPLY SELECTION TO ACTIVE SCREEN ---
if matching_tag_ids.Count > 0:
    uidoc.Selection.SetElementIds(matching_tag_ids)
    
    forms.alert(
        "Successfully selected {} matching tag(s) in the active view!".format(matching_tag_ids.Count),
        title="Selection Complete"
    )
else:
    forms.alert(
        "No matching tag instances were found visible in the active screen.",
        title="No Matching Tags Found"
    )