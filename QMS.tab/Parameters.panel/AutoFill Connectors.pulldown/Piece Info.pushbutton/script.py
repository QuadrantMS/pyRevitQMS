# -*- coding: utf-8 -*-
import os
import System
from collections import deque
from pyrevit import forms, script
from Autodesk.Revit.DB import (
    BuiltInCategory,
    ElementMulticategoryFilter,
    FilteredElementCollector,
    Transaction,
)
from Autodesk.Revit.Exceptions import OperationCanceledException
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType

doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument

# TARGET PARAMETER NAMES
PARAM_ADDITIONAL_INFO = "QMS_Duct_Additional_Info"
PARAM_MATERIAL = "QMS_Duct_Material"
PARAM_SPECIFICATION = "QMS_Duct_Specification"

# CONFIG KEYS FOR PYREVIT CONFIG MANAGER
CFG_KEY_INFO = "last_additional_info"
CFG_KEY_MAT = "last_material"
CFG_KEY_SPEC = "last_specification"


# 1. SELECTION FILTER
class DuctAndFittingSelectionFilter(ISelectionFilter):
    """Filters viewport selection strictly to Ducts and Duct Fittings."""

    def AllowElement(self, element):
        if element.Category:
            cat_id = element.Category.Id.IntegerValue
            if cat_id in [
                int(BuiltInCategory.OST_DuctCurves),
                int(BuiltInCategory.OST_DuctFitting),
            ]:
                return True
        return False

    def AllowReference(self, reference, point):
        return False


# 2. HELPER TO UPDATE PARAMETERS ON SINGLE ELEMENT
def update_element_parameters(
    element, val_info, val_mat, val_spec, transaction_msg="Autofill Piece Info"
):
    """Writes values to target parameters wrapped in a Revit Transaction."""
    p_info = element.LookupParameter(PARAM_ADDITIONAL_INFO)
    p_mat = element.LookupParameter(PARAM_MATERIAL)
    p_spec = element.LookupParameter(PARAM_SPECIFICATION)

    if p_info or p_mat or p_spec:
        t = Transaction(doc, transaction_msg)
        t.Start()
        if p_info and not p_info.IsReadOnly:
            p_info.Set(val_info)
        if p_mat and not p_mat.IsReadOnly:
            p_mat.Set(val_mat)
        if p_spec and not p_spec.IsReadOnly:
            p_spec.Set(val_spec)
        t.Commit()
        return True
    return False


# 3. HELPER TO GET CONNECTED NEIGHBORS
def get_connected_neighbors(element):
    """Retrieves physically connected Duct, Fitting, or Accessory neighbors."""
    neighbors = []
    conn_mgr = None

    if hasattr(element, "ConnectorManager") and element.ConnectorManager:
        conn_mgr = element.ConnectorManager
    elif (
        hasattr(element, "MEPModel")
        and element.MEPModel
        and element.MEPModel.ConnectorManager
    ):
        conn_mgr = element.MEPModel.ConnectorManager

    if not conn_mgr:
        return neighbors

    valid_traversal_categories = [
        int(BuiltInCategory.OST_DuctCurves),
        int(BuiltInCategory.OST_DuctFitting),
        int(BuiltInCategory.OST_DuctAccessory),
    ]

    for connector in conn_mgr.Connectors:
        if not connector.IsConnected:
            continue
        for ref in connector.AllRefs:
            if ref.Owner.Id == element.Id or str(ref.ConnectorType) == "Logical":
                continue
            next_elem = ref.Owner
            cat_id = (
                next_elem.Category.Id.IntegerValue if next_elem.Category else 0
            )
            if cat_id in valid_traversal_categories:
                neighbors.append(next_elem)

    return neighbors


# 4. PATHFINDING ALGORITHM (BFS)
def find_path_between_elements(start_elem, end_elem):
    """Finds connected path between start_elem and end_elem using BFS."""
    queue = deque([[start_elem.Id]])
    visited = {start_elem.Id}

    while queue:
        path = queue.popleft()
        current_id = path[-1]

        if current_id == end_elem.Id:
            return path

        current_elem = doc.GetElement(current_id)
        for neighbor in get_connected_neighbors(current_elem):
            if neighbor.Id not in visited:
                visited.add(neighbor.Id)
                new_path = list(path)
                new_path.append(neighbor.Id)
                queue.append(new_path)

    return None


# 5. WPF WINDOW CONTROLLER
class PieceInfoWindow(forms.WPFWindow):

    def __init__(self, xaml_file_name):
        script_dir = os.path.dirname(__file__)
        xaml_path = os.path.join(script_dir, xaml_file_name)

        forms.WPFWindow.__init__(self, xaml_path)

        self.action_type = None
        self.cfg = script.get_config()

        # Load saved settings into UI TextBoxes
        self.txt_AdditionalInfo.Text = self.cfg.get_option(CFG_KEY_INFO, "")
        self.txt_Material.Text = self.cfg.get_option(CFG_KEY_MAT, "")
        self.txt_Specification.Text = self.cfg.get_option(CFG_KEY_SPEC, "")

        self.additional_info = ""
        self.material = ""
        self.specification = ""

        # Event Handlers Wiring
        self.btn_PickElements.Click += self.on_pick_elements
        self.btn_PickRun.Click += self.on_pick_run
        self.btn_ActiveView.Click += self.on_active_view
        self.btn_Cancel.Click += self.on_cancel

    def on_pick_elements(self, sender, e):
        self.action_type = "PICK_ELEMENTS"
        self._save_inputs_and_close()

    def on_pick_run(self, sender, e):
        self.action_type = "PICK_RUN"
        self._save_inputs_and_close()

    def on_active_view(self, sender, e):
        self.action_type = "ACTIVE_VIEW"
        self._save_inputs_and_close()

    def on_cancel(self, sender, e):
        self.action_type = "CANCEL"
        self.Close()

    def _save_inputs_and_close(self):
        # Read text values from UI
        self.additional_info = self.txt_AdditionalInfo.Text
        self.material = self.txt_Material.Text
        self.specification = self.txt_Specification.Text

        # Save to pyRevit User Config
        self.cfg.set_option(CFG_KEY_INFO, self.additional_info)
        self.cfg.set_option(CFG_KEY_MAT, self.material)
        self.cfg.set_option(CFG_KEY_SPEC, self.specification)
        script.save_config()

        self.Close()


# 6. ACTION EXECUTION FUNCTIONS
def execute_pick_elements_live(val_info, val_mat, val_spec):
    """Continuous pick mode: Updates element immediately upon click until ESC is pressed."""
    filter_inst = DuctAndFittingSelectionFilter()
    count = 0

    while True:
        try:
            ref = uidoc.Selection.PickObject(
                ObjectType.Element,
                filter_inst,
                "Click a Duct or Fitting to autofill immediately (Press ESC to stop)",
            )
            elem = doc.GetElement(ref.ElementId)

            if update_element_parameters(
                elem,
                val_info,
                val_mat,
                val_spec,
                "Autofill Single Piece Info",
            ):
                count += 1

        except OperationCanceledException:
            break
        except Exception:
            break

    if count > 0:
        forms.alert(
            "Updated {} element(s) individually.".format(count),
            title="Pick Elements Complete",
            warn_icon=False,
        )


def execute_pick_run(val_info, val_mat, val_spec):
    """Pick start and end elements, then update path."""
    filter_inst = DuctAndFittingSelectionFilter()

    try:
        ref_start = uidoc.Selection.PickObject(
            ObjectType.Element,
            filter_inst,
            "Pick START element of the duct run",
        )
        start_elem = doc.GetElement(ref_start.ElementId)

        ref_end = uidoc.Selection.PickObject(
            ObjectType.Element,
            filter_inst,
            "Pick END element of the duct run",
        )
        end_elem = doc.GetElement(ref_end.ElementId)
    except Exception:
        return

    path_ids = find_path_between_elements(start_elem, end_elem)

    if not path_ids:
        forms.alert(
            "Please pick the start and end of duct run!",
            title="Invalid Selection",
            warn_icon=True,
        )
        return

    allowed_categories = [
        int(BuiltInCategory.OST_DuctCurves),
        int(BuiltInCategory.OST_DuctFitting),
    ]

    updated_count = 0
    t = Transaction(doc, "Autofill Duct Run Piece Info")
    t.Start()

    for el_id in path_ids:
        el = doc.GetElement(el_id)
        if (
            el
            and el.Category
            and el.Category.Id.IntegerValue in allowed_categories
        ):
            p_info = el.LookupParameter(PARAM_ADDITIONAL_INFO)
            p_mat = el.LookupParameter(PARAM_MATERIAL)
            p_spec = el.LookupParameter(PARAM_SPECIFICATION)

            if p_info and not p_info.IsReadOnly:
                p_info.Set(val_info)
            if p_mat and not p_mat.IsReadOnly:
                p_mat.Set(val_mat)
            if p_spec and not p_spec.IsReadOnly:
                p_spec.Set(val_spec)
            updated_count += 1

    t.Commit()

    forms.alert(
        "Successfully updated {} element(s) along the duct run!".format(
            updated_count
        ),
        title="Pick Run Complete",
        warn_icon=False,
    )


def execute_active_view(val_info, val_mat, val_spec):
    """Collects and updates all Ducts and Duct Fittings in the active view."""
    active_view_id = doc.ActiveView.Id

    categories = System.Collections.Generic.List[BuiltInCategory]()
    categories.Add(BuiltInCategory.OST_DuctCurves)
    categories.Add(BuiltInCategory.OST_DuctFitting)

    cat_filter = ElementMulticategoryFilter(categories)

    collector = (
        FilteredElementCollector(doc, active_view_id)
        .WherePasses(cat_filter)
        .WhereElementIsNotElementType()
    )

    updated_count = 0
    t = Transaction(doc, "Autofill Active View Piece Info")
    t.Start()

    for elem in collector:
        p_info = elem.LookupParameter(PARAM_ADDITIONAL_INFO)
        p_mat = elem.LookupParameter(PARAM_MATERIAL)
        p_spec = elem.LookupParameter(PARAM_SPECIFICATION)

        if p_info or p_mat or p_spec:
            if p_info and not p_info.IsReadOnly:
                p_info.Set(val_info)
            if p_mat and not p_mat.IsReadOnly:
                p_mat.Set(val_spec)
            if p_spec and not p_spec.IsReadOnly:
                p_spec.Set(val_spec)
            updated_count += 1

    t.Commit()

    forms.alert(
        "Successfully updated {} duct(s) and fitting(s) in Active View!".format(
            updated_count
        ),
        title="Active View Complete",
        warn_icon=False,
    )


# 7. MAIN EXECUTION FLOW
win = PieceInfoWindow("UI.xaml")
win.show_dialog()

if win.action_type == "PICK_ELEMENTS":
    execute_pick_elements_live(
        win.additional_info, win.material, win.specification
    )
elif win.action_type == "PICK_RUN":
    execute_pick_run(win.additional_info, win.material, win.specification)
elif win.action_type == "ACTIVE_VIEW":
    execute_active_view(win.additional_info, win.material, win.specification)