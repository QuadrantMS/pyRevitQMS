# -*- coding: utf-8 -*-
import sys
import os
import math
import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

from pyrevit import revit, DB, forms, script
from Autodesk.Revit.UI import Selection

doc = revit.doc
uidoc = revit.uidoc

# ==============================================================================
# TOOL CONFIGURATION (WALL PENETRATION WITH AIR TERMINALS & FIRE WALL FOR ALL DUCTS)
# ==============================================================================
LOCKED_HOST_CATEGORY_NAME = "Walls"

CURRENT_MODEL_OPTION = "<Current Active Model>"
BASE_MEP_CATEGORIES = [
    DB.BuiltInCategory.OST_DuctCurves,
    DB.BuiltInCategory.OST_PipeCurves,
    DB.BuiltInCategory.OST_DuctAccessory,
    DB.BuiltInCategory.OST_PipeAccessory,
    DB.BuiltInCategory.OST_DuctFitting,
    DB.BuiltInCategory.OST_PipeFitting
]

CFG_KEY = "PenetrationGeneratorSettings_v4_{}".format(LOCKED_HOST_CATEGORY_NAME)
cfg = script.get_config(CFG_KEY)


# --- UNIT HELPERS ---
def mm_to_feet(mm_val):
    try:
        if hasattr(DB, "UnitUtils") and hasattr(DB, "UnitTypeId"):
            return DB.UnitUtils.ConvertToInternalUnits(mm_val, DB.UnitTypeId.Millimeters)
        elif hasattr(DB, "DisplayUnitType"):
            return DB.UnitUtils.ConvertToInternalUnits(mm_val, DB.DisplayUnitType.DUT_MILLIMETERS)
    except Exception:
        pass
    return float(mm_val) / 304.8


def feet_to_mm(ft_val):
    try:
        if hasattr(DB, "UnitUtils") and hasattr(DB, "UnitTypeId"):
            return DB.UnitUtils.ConvertFromInternalUnits(ft_val, DB.UnitTypeId.Millimeters)
        elif hasattr(DB, "DisplayUnitType"):
            return DB.UnitUtils.ConvertFromInternalUnits(ft_val, DB.DisplayUnitType.DUT_MILLIMETERS)
    except Exception:
        pass
    return float(ft_val) * 304.8


def is_union_fitting(elem):
    try:
        if hasattr(elem, "MEPModel") and elem.MEPModel:
            if hasattr(elem.MEPModel, "PartType"):
                part_type = elem.MEPModel.PartType
                if str(part_type) == "Union" or int(part_type) == 7:
                    return True

        if hasattr(elem, "Symbol") and elem.Symbol and hasattr(elem.Symbol, "Family"):
            fam = elem.Symbol.Family
            p_part_type = fam.get_Parameter(DB.BuiltInParameter.FAMILY_CONTENT_PART_TYPE)
            if p_part_type:
                val_str = p_part_type.AsValueString() or p_part_type.AsString() or ""
                if "union" in val_str.lower() or p_part_type.AsInteger() == 7:
                    return True

        elem_type = elem.Document.GetElement(elem.GetTypeId())
        if elem_type:
            p_part_type = elem_type.LookupParameter("Part Type") or elem_type.get_Parameter(DB.BuiltInParameter.FAMILY_CONTENT_PART_TYPE)
            if p_part_type:
                val_str = p_part_type.AsValueString() or p_part_type.AsString() or ""
                if "union" in val_str.lower() or p_part_type.AsInteger() == 7:
                    return True
    except Exception:
        pass
    return False


def is_penetration_instance(elem, peno_symbol_ids):
    """ Prevents Generic Model scanning from picking up existing Penetration families """
    try:
        if not elem or not elem.Category:
            return False
        if elem.Category.Id.IntegerValue != int(DB.BuiltInCategory.OST_GenericModel):
            return False
        if elem.GetTypeId() in peno_symbol_ids:
            return True
        if elem.LookupParameter("BarrierThickness") is not None or elem.LookupParameter("Barrier Thickness") is not None:
            return True
    except Exception:
        pass
    return False


# --- SELECTION FILTER ---
class MEPSelectionFilter(Selection.ISelectionFilter):
    def __init__(self, allowed_categories, peno_symbol_ids):
        self.allowed_cat_ids = [int(c) for c in allowed_categories]
        self.peno_symbol_ids = peno_symbol_ids

    def AllowElement(self, elem):
        if not elem or not elem.Category:
            return False
        if elem.Category.Id.IntegerValue not in self.allowed_cat_ids:
            return False
        if is_union_fitting(elem):
            return False
        if is_penetration_instance(elem, self.peno_symbol_ids):
            return False
        return True

    def AllowReference(self, reference, point):
        return True


# --- UI WINDOW ---
class PenetrationUI(forms.WPFWindow):
    def __init__(self, xaml_file_name):
        forms.WPFWindow.__init__(self, xaml_file_name)

        self.mode = None
        self.target_doc = None
        self.target_link_inst = None
        self.selected_category_bic = DB.BuiltInCategory.OST_Walls
        self.type_filter_keyword = ""

        self.is_fire_wall = False
        self.enable_mep = True
        self.enable_air_terminal = True
        self.enable_generic_model = False

        self.config = {}

        self.btn_PickElements.Click += self.OnPickElements
        self.btn_EntireView.Click += self.OnEntireView
        self.btn_Cancel.Click += self.OnCancel

        self.chk_EnableMep.Checked += self.OnScopeChanged
        self.chk_EnableMep.Unchecked += self.OnScopeChanged
        self.chk_EnableAirTerminal.Checked += self.OnScopeChanged
        self.chk_EnableAirTerminal.Unchecked += self.OnScopeChanged
        self.chk_EnableGenericModel.Checked += self.OnScopeChanged
        self.chk_EnableGenericModel.Unchecked += self.OnScopeChanged

        self.chk_FireWall.Checked += self.OnFireWallChanged
        self.chk_FireWall.Unchecked += self.OnFireWallChanged

        self.cb_SymbolRect.KeyUp += self.OnComboKeyUp
        self.cb_SymbolRoundDuct.KeyUp += self.OnComboKeyUp
        self.cb_SymbolPipe.KeyUp += self.OnComboKeyUp
        self.cb_SymbolAirTerminalRect.KeyUp += self.OnComboKeyUp
        self.cb_SymbolAirTerminalRound.KeyUp += self.OnComboKeyUp
        self.cb_SymbolGenericModel.KeyUp += self.OnComboKeyUp

        self.populate_models()
        self.populate_symbols()
        self.load_user_defaults()
        self.sync_ui_states()

    def populate_models(self):
        self.cb_LinkModel.Items.Add(CURRENT_MODEL_OPTION)
        self.model_map = {CURRENT_MODEL_OPTION: (doc, None)}

        try:
            links = DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance).ToElements()
            for l in links:
                try:
                    l_doc = l.GetLinkDocument()
                    if not l_doc:
                        continue
                    raw_name = l.GetName() if hasattr(l, "GetName") else l_doc.Title
                    clean_name = raw_name.split(" : ")[0]
                    display_name = "[LINK] " + clean_name
                    counter = 1
                    base_display_name = display_name
                    while display_name in self.model_map:
                        display_name = "{} ({})".format(base_display_name, counter)
                        counter += 1
                    self.model_map[display_name] = (l_doc, l)
                    self.cb_LinkModel.Items.Add(display_name)
                except Exception:
                    pass
        except Exception:
            pass

        self.cb_LinkModel.SelectedIndex = 0

    def populate_symbols(self):
        symbols = DB.FilteredElementCollector(doc)\
                    .OfCategory(DB.BuiltInCategory.OST_GenericModel)\
                    .OfClass(DB.FamilySymbol)\
                    .ToElements()

        self.symbol_map = {
            "{} : {}".format(s.Family.Name, DB.Element.Name.__get__(s)): s
            for s in symbols
        }
        self.all_symbol_names = sorted(self.symbol_map.keys())

        combos = [
            self.cb_SymbolRect, self.cb_SymbolRoundDuct, self.cb_SymbolPipe,
            self.cb_SymbolAirTerminalRect, self.cb_SymbolAirTerminalRound,
            self.cb_SymbolGenericModel
        ]
        for cb in combos:
            self.filter_combobox(cb, "", set_drop_open=False)

    def auto_match_qms_symbol(self, shape_keywords):
        host_kw = "wall"
        best_match = None
        for name in self.all_symbol_names:
            low = name.lower()
            if "peno" in low and host_kw in low:
                if any(sk in low for sk in shape_keywords):
                    if "qms" in low:
                        return name
                    if not best_match:
                        best_match = name
        return best_match

    def set_rotation_combo(self, cb_rot, rot_text):
        for item in cb_rot.Items:
            if rot_text and rot_text.split("°")[0] in item.Content.ToString():
                cb_rot.SelectedItem = item
                break

    def sync_ui_states(self):
        mep_en = bool(self.chk_EnableMep.IsChecked)
        self.cb_SymbolRect.IsEnabled = mep_en
        self.cb_RotationRect.IsEnabled = mep_en
        self.cb_SymbolRoundDuct.IsEnabled = mep_en
        self.cb_RotationRoundDuct.IsEnabled = mep_en
        self.cb_SymbolPipe.IsEnabled = mep_en
        self.tb_OffsetPipe.IsEnabled = mep_en
        self.cb_RotationPipe.IsEnabled = mep_en

        # Fire Wall ON -> Auto (25 / 50) applies to BOTH Rect Ducts & DA and Round Ducts & DA
        is_fw = bool(self.chk_FireWall.IsChecked)
        self.tb_OffsetRect.IsEnabled = mep_en and (not is_fw)
        self.tb_OffsetRoundDuct.IsEnabled = mep_en and (not is_fw)

        if is_fw:
            self.tb_OffsetRect.Text = "Auto (25 / 50)"
            self.tb_OffsetRoundDuct.Text = "Auto (25 / 50)"
        else:
            if "Auto" in self.tb_OffsetRect.Text:
                self.tb_OffsetRect.Text = "100"
            if "Auto" in self.tb_OffsetRoundDuct.Text:
                self.tb_OffsetRoundDuct.Text = "100"

        at_en = bool(self.chk_EnableAirTerminal.IsChecked)
        self.cb_SymbolAirTerminalRect.IsEnabled = at_en
        self.tb_OffsetAirTerminal.IsEnabled = at_en
        self.cb_RotationAirTerminalRect.IsEnabled = at_en
        self.cb_SymbolAirTerminalRound.IsEnabled = at_en
        self.tb_OffsetAirTerminalRound.IsEnabled = at_en
        self.cb_RotationAirTerminalRound.IsEnabled = at_en

        gm_en = bool(self.chk_EnableGenericModel.IsChecked)
        self.cb_SymbolGenericModel.IsEnabled = gm_en
        self.tb_OffsetGenericModel.IsEnabled = gm_en
        self.cb_RotationGenericModel.IsEnabled = gm_en

    def OnScopeChanged(self, sender, args):
        self.sync_ui_states()

    def OnFireWallChanged(self, sender, args):
        self.sync_ui_states()

    def load_user_defaults(self):
        missing_required_symbol = False
        try:
            last_model = cfg.get_option("last_host_model", None)
            if last_model and str(last_model) in self.model_map:
                self.cb_LinkModel.SelectedItem = str(last_model)
            else:
                self.cb_LinkModel.SelectedIndex = 0

            last_type_kw = cfg.get_option("last_type_kw", None)
            if last_type_kw is not None:
                self.tb_TypeFilterKeyword.Text = str(last_type_kw)

            self.chk_EnableMep.IsChecked = bool(cfg.get_option("last_enable_mep", True))
            self.chk_EnableAirTerminal.IsChecked = bool(cfg.get_option("last_enable_at", True))
            self.chk_EnableGenericModel.IsChecked = bool(cfg.get_option("last_enable_gm", False))
            self.chk_FireWall.IsChecked = bool(cfg.get_option("last_fire_wall", False))

            fields = [
                ("rect", self.cb_SymbolRect, self.tb_OffsetRect, self.cb_RotationRect, ["square", "rect"], True),
                ("rduct", self.cb_SymbolRoundDuct, self.tb_OffsetRoundDuct, self.cb_RotationRoundDuct, ["round", "circ"], True),
                ("pipe", self.cb_SymbolPipe, self.tb_OffsetPipe, self.cb_RotationPipe, ["round", "pipe", "circ"], True),
                ("at_rect", self.cb_SymbolAirTerminalRect, self.tb_OffsetAirTerminal, self.cb_RotationAirTerminalRect, ["square", "rect"], True),
                ("at_round", self.cb_SymbolAirTerminalRound, self.tb_OffsetAirTerminalRound, self.cb_RotationAirTerminalRound, ["round", "circ"], True),
                ("gm", self.cb_SymbolGenericModel, self.tb_OffsetGenericModel, self.cb_RotationGenericModel, ["square", "rect"], False),
            ]

            for key, cb_sym, tb_off, cb_rot, shape_kws, is_primary in fields:
                sym = cfg.get_option("last_sym_" + key, None)
                if sym and sym in self.symbol_map:
                    cb_sym.Text = sym
                else:
                    auto_sym = self.auto_match_qms_symbol(shape_kws)
                    if auto_sym:
                        cb_sym.Text = auto_sym
                    elif is_primary:
                        missing_required_symbol = True

                off = cfg.get_option("last_overall_off_" + key, None)
                if off:
                    tb_off.Text = str(off)

                rot = cfg.get_option("last_rot_" + key, None)
                if rot:
                    self.set_rotation_combo(cb_rot, str(rot))

            if missing_required_symbol:
                self.exp_AdvancedSettings.IsExpanded = True
        except Exception:
            pass

    def save_user_defaults(self):
        try:
            if self.cb_LinkModel.SelectedItem:
                cfg.set_option("last_host_model", str(self.cb_LinkModel.SelectedItem))

            cfg.set_option("last_type_kw", self.tb_TypeFilterKeyword.Text)
            cfg.set_option("last_enable_mep", bool(self.chk_EnableMep.IsChecked))
            cfg.set_option("last_enable_at", bool(self.chk_EnableAirTerminal.IsChecked))
            cfg.set_option("last_enable_gm", bool(self.chk_EnableGenericModel.IsChecked))
            cfg.set_option("last_fire_wall", bool(self.chk_FireWall.IsChecked))

            fields = [
                ("rect", self.cb_SymbolRect, self.tb_OffsetRect, self.cb_RotationRect),
                ("rduct", self.cb_SymbolRoundDuct, self.tb_OffsetRoundDuct, self.cb_RotationRoundDuct),
                ("pipe", self.cb_SymbolPipe, self.tb_OffsetPipe, self.cb_RotationPipe),
                ("at_rect", self.cb_SymbolAirTerminalRect, self.tb_OffsetAirTerminal, self.cb_RotationAirTerminalRect),
                ("at_round", self.cb_SymbolAirTerminalRound, self.tb_OffsetAirTerminalRound, self.cb_RotationAirTerminalRound),
                ("gm", self.cb_SymbolGenericModel, self.tb_OffsetGenericModel, self.cb_RotationGenericModel),
            ]

            for key, cb_sym, tb_off, cb_rot in fields:
                cfg.set_option("last_sym_" + key, cb_sym.Text)
                if "Auto" not in tb_off.Text:
                    cfg.set_option("last_overall_off_" + key, tb_off.Text)
                rot_val = cb_rot.SelectedItem.Content.ToString() if cb_rot.SelectedItem else cb_rot.Text
                cfg.set_option("last_rot_" + key, rot_val)

            script.save_config()
        except Exception:
            pass

    def filter_combobox(self, cb_element, query, set_drop_open=True):
        current_text = cb_element.Text
        cb_element.Items.Clear()
        query = query.strip().lower()

        for name in self.all_symbol_names:
            if not query or query in name.lower():
                cb_element.Items.Add(name)

        if set_drop_open:
            cb_element.IsDropDownOpen = True
        cb_element.Text = current_text

        tb = cb_element.Template.FindName("PART_EditableTextBox", cb_element)
        if tb:
            tb.CaretIndex = len(current_text)

    def OnComboKeyUp(self, sender, args):
        if args.Key.ToString() not in ["Up", "Down", "Enter", "Escape", "Tab"]:
            self.filter_combobox(sender, sender.Text)

    def parse_profile_config(self, cb_sym, tb_off, cb_rot, label, skip_offset_parse=False):
        sym_text = cb_sym.Text
        if not sym_text or sym_text not in self.symbol_map:
            self.exp_AdvancedSettings.IsExpanded = True
            forms.alert("Please select a valid Penetration Family Type for " + label + " in 3. Settings.")
            return None

        offset_feet = 0.0
        if not skip_offset_parse:
            try:
                offset_feet = mm_to_feet(float(tb_off.Text))
            except ValueError:
                self.exp_AdvancedSettings.IsExpanded = True
                forms.alert("Invalid Overall Offset Value for " + label)
                return None

        rot_text = cb_rot.SelectedItem.Content.ToString() if cb_rot.SelectedItem else (cb_rot.Text or "")
        rot_deg = 90.0 if "90" in rot_text else 0.0

        return {
            "symbol": self.symbol_map[sym_text],
            "offset_feet": offset_feet,
            "rotation_rad": math.radians(rot_deg)
        }

    def parse_inputs(self):
        selected_model = self.cb_LinkModel.SelectedItem
        if not selected_model or selected_model not in self.model_map:
            forms.alert("Please select a valid host model.")
            return False

        self.target_doc, self.target_link_inst = self.model_map[selected_model]
        self.selected_category_bic = DB.BuiltInCategory.OST_Walls
        self.type_filter_keyword = self.tb_TypeFilterKeyword.Text.strip().lower()

        self.enable_mep = bool(self.chk_EnableMep.IsChecked)
        self.enable_air_terminal = bool(self.chk_EnableAirTerminal.IsChecked)
        self.enable_generic_model = bool(self.chk_EnableGenericModel.IsChecked)

        if not (self.enable_mep or self.enable_air_terminal or self.enable_generic_model):
            forms.alert("Please tick at least one element type in Section 2.")
            return False

        self.is_fire_wall = bool(self.chk_FireWall.IsChecked)

        if self.enable_mep:
            rect_cfg = self.parse_profile_config(
                self.cb_SymbolRect, self.tb_OffsetRect, self.cb_RotationRect,
                "Rect Ducts & DA", skip_offset_parse=self.is_fire_wall
            )
            rduct_cfg = self.parse_profile_config(
                self.cb_SymbolRoundDuct, self.tb_OffsetRoundDuct, self.cb_RotationRoundDuct,
                "Round Ducts & DA", skip_offset_parse=self.is_fire_wall
            )
            pipe_cfg = self.parse_profile_config(
                self.cb_SymbolPipe, self.tb_OffsetPipe, self.cb_RotationPipe, "Round Pipes"
            )
            if not (rect_cfg and rduct_cfg and pipe_cfg):
                return False
            self.config["RECT"] = rect_cfg
            self.config["ROUND_DUCT"] = rduct_cfg
            self.config["PIPE"] = pipe_cfg

        if self.enable_air_terminal:
            at_rect_cfg = self.parse_profile_config(
                self.cb_SymbolAirTerminalRect, self.tb_OffsetAirTerminal, self.cb_RotationAirTerminalRect, "Air Terminal (Rect)"
            )
            at_round_cfg = self.parse_profile_config(
                self.cb_SymbolAirTerminalRound, self.tb_OffsetAirTerminalRound, self.cb_RotationAirTerminalRound, "Air Terminal (Swirl)"
            )
            if not (at_rect_cfg and at_round_cfg):
                return False
            self.config["AIR_TERMINAL_RECT"] = at_rect_cfg
            self.config["AIR_TERMINAL_ROUND"] = at_round_cfg

        if self.enable_generic_model:
            gm_cfg = self.parse_profile_config(
                self.cb_SymbolGenericModel, self.tb_OffsetGenericModel, self.cb_RotationGenericModel, "Generic Models"
            )
            if not gm_cfg:
                return False
            self.config["GENERIC_MODEL"] = gm_cfg

        self.save_user_defaults()
        return True

    def OnPickElements(self, sender, args):
        if self.parse_inputs():
            self.mode = "PICK_ELEMENTS"
            self.Close()

    def OnEntireView(self, sender, args):
        if self.parse_inputs():
            self.mode = "ENTIRE_VIEW"
            self.Close()

    def OnCancel(self, sender, args):
        self.mode = None
        self.Close()


# --- HELPER FUNCTIONS ---

def get_element_type_name(elem):
    try:
        elem_type = elem.Document.GetElement(elem.GetTypeId())
        return DB.Element.Name.__get__(elem_type).lower() if elem_type else ""
    except Exception:
        return ""


def get_element_family_and_type_name(elem):
    try:
        elem_type = elem.Document.GetElement(elem.GetTypeId())
        if not elem_type:
            return ""
        fam_name = elem_type.FamilyName if hasattr(elem_type, "FamilyName") else ""
        type_name = DB.Element.Name.__get__(elem_type)
        return "{} {}".format(fam_name, type_name).lower()
    except Exception:
        return ""


def get_param_double_instance_or_type(elem, param_name):
    try:
        p = elem.LookupParameter(param_name)
        if p and p.HasValue:
            val = p.AsDouble()
            if val > 0:
                return val
        elem_type = elem.Document.GetElement(elem.GetTypeId())
        if elem_type:
            p_type = elem_type.LookupParameter(param_name)
            if p_type and p_type.HasValue:
                val = p_type.AsDouble()
                if val > 0:
                    return val
    except Exception:
        pass
    return 0.0


def get_insulation_thickness_2x(mep_elem):
    try:
        p_insul = mep_elem.get_Parameter(DB.BuiltInParameter.RBS_REFERENCE_INSULATION_THICKNESS) or \
                  mep_elem.LookupParameter("Insulation Thickness")
        if p_insul and p_insul.HasValue:
            val = p_insul.AsDouble()
            if val > 0:
                return val * 2.0
    except Exception:
        pass
    return 0.0


def is_refrigerant_pipe(mep_elem):
    try:
        p_sys = mep_elem.get_Parameter(DB.BuiltInParameter.RBS_PIPING_SYSTEM_TYPE_PARAM) or \
                mep_elem.LookupParameter("System Type")
        if p_sys:
            sys_str = p_sys.AsValueString() or p_sys.AsString() or ""
            if "refrigerant" in sys_str.lower():
                return True
        p_name = mep_elem.get_Parameter(DB.BuiltInParameter.RBS_SYSTEM_NAME_PARAM)
        if p_name:
            name_str = p_name.AsString() or ""
            if "refrigerant" in name_str.lower():
                return True
    except Exception:
        pass
    return False


def get_element_solid(elem, transform=None):
    opt = DB.Options()
    opt.ComputeReferences = True
    opt.DetailLevel = DB.ViewDetailLevel.Fine

    geom_elem = elem.get_Geometry(opt)
    if not geom_elem:
        return None

    solids = []
    for g_obj in geom_elem:
        if isinstance(g_obj, DB.Solid) and g_obj.Volume > 0:
            solids.append(g_obj)
        elif isinstance(g_obj, DB.GeometryInstance):
            for g in g_obj.GetInstanceGeometry():
                if isinstance(g, DB.Solid) and g.Volume > 0:
                    solids.append(g)

    if not solids:
        return None

    unified_solid = solids[0]
    for s in solids[1:]:
        try:
            unified_solid = DB.BooleanOperationsUtils.ExecuteBooleanOperation(
                unified_solid, s, DB.BooleanOperationsType.Union
            )
        except Exception:
            pass

    if transform and not transform.IsIdentity:
        unified_solid = DB.SolidUtils.CreateTransformed(unified_solid, transform)

    return unified_solid


def get_wall_thickness(wall_elem):
    """ Reads exact Wall Thickness for BarrierThickness """
    try:
        elem_type = wall_elem.Document.GetElement(wall_elem.GetTypeId())
        if elem_type:
            p_w = elem_type.get_Parameter(DB.BuiltInParameter.WALL_ATTR_WIDTH_PARAM)
            if p_w and p_w.HasValue and p_w.AsDouble() > 0:
                return p_w.AsDouble()

            if hasattr(elem_type, "Width") and elem_type.Width > 0:
                return elem_type.Width

            if hasattr(elem_type, "GetCompoundStructure"):
                cs = elem_type.GetCompoundStructure()
                if cs and cs.GetWidth() > 0:
                    return cs.GetWidth()

            p_thick = elem_type.LookupParameter("Thickness") or wall_elem.LookupParameter("Thickness")
            if p_thick and p_thick.HasValue and p_thick.AsDouble() > 0:
                return p_thick.AsDouble()
    except Exception:
        pass
    return 0.0


def get_mep_profile_type(mep_elem):
    cat_id = mep_elem.Category.Id.IntegerValue
    if cat_id == int(DB.BuiltInCategory.OST_DuctTerminal):
        return "AIR_TERMINAL"
    if cat_id == int(DB.BuiltInCategory.OST_GenericModel):
        return "GENERIC_MODEL"

    pipe_cats = [int(c) for c in [
        DB.BuiltInCategory.OST_PipeCurves,
        DB.BuiltInCategory.OST_PipeAccessory,
        DB.BuiltInCategory.OST_PipeFitting
    ]]
    if cat_id in pipe_cats:
        return "PIPE"

    p_dia = mep_elem.LookupParameter("Diameter") or \
            mep_elem.get_Parameter(DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM) or \
            mep_elem.get_Parameter(DB.BuiltInParameter.RBS_CURVE_DIAMETER_PARAM)
    return "ROUND_DUCT" if p_dia else "RECT"


def get_air_terminal_dimensions(elem):
    neck1 = get_param_double_instance_or_type(elem, "Neck1")
    neck2 = get_param_double_instance_or_type(elem, "Neck2")
    full_name = get_element_family_and_type_name(elem)

    is_round_keyword = any(kw in full_name for kw in ["round", "swirl", "circular", "dia"])

    if neck1 > 0 and (neck2 <= 0 or is_round_keyword):
        return neck1, neck1, neck1, True

    if neck1 > 0 and neck2 > 0:
        return neck1, neck2, 0.0, False

    try:
        conn_mgr = None
        if hasattr(elem, "MEPModel") and elem.MEPModel and elem.MEPModel.ConnectorManager:
            conn_mgr = elem.MEPModel.ConnectorManager
        if conn_mgr:
            for conn in conn_mgr.Connectors:
                if conn.Shape == DB.ConnectorProfileType.Round:
                    dia = conn.Radius * 2.0
                    return dia, dia, dia, True
                elif conn.Shape == DB.ConnectorProfileType.Rectangular:
                    return conn.Width, conn.Height, 0.0, False
    except Exception:
        pass

    bb = elem.get_BoundingBox(None)
    if bb:
        dx = abs(bb.Max.X - bb.Min.X)
        dy = abs(bb.Max.Y - bb.Min.Y)
        dz = abs(bb.Max.Z - bb.Min.Z)
        w, h = max(dx, dy), dz
        if is_round_keyword:
            dia = max(w, h)
            return dia, dia, dia, True
        return w, h, 0.0, False

    return 0.0, 0.0, 0.0, False


def get_generic_model_dimensions(elem):
    w = get_param_double_instance_or_type(elem, "Width")
    h = get_param_double_instance_or_type(elem, "Height")
    if w > 0 and h > 0:
        return w, h, 0.0

    try:
        opt = DB.Options()
        geom = elem.get_Geometry(opt)
        if geom:
            for g in geom:
                if isinstance(g, DB.GeometryInstance):
                    sym_bb = g.GetSymbolGeometry().GetBoundingBox()
                    if sym_bb:
                        sx = abs(sym_bb.Max.X - sym_bb.Min.X)
                        sy = abs(sym_bb.Max.Y - sym_bb.Min.Y)
                        sz = abs(sym_bb.Max.Z - sym_bb.Min.Z)
                        return max(sx, sy), sz, 0.0
    except Exception:
        pass

    bb = elem.get_BoundingBox(None)
    if bb:
        dx = abs(bb.Max.X - bb.Min.X)
        dy = abs(bb.Max.Y - bb.Min.Y)
        dz = abs(bb.Max.Z - bb.Min.Z)
        return max(dx, dy), dz, 0.0

    return 0.0, 0.0, 0.0


def get_mep_dimensions(mep_elem):
    p_w = mep_elem.LookupParameter("Width") or mep_elem.get_Parameter(DB.BuiltInParameter.RBS_CURVE_WIDTH_PARAM)
    p_h = mep_elem.LookupParameter("Height") or mep_elem.get_Parameter(DB.BuiltInParameter.RBS_CURVE_HEIGHT_PARAM)
    p_dia = mep_elem.LookupParameter("Diameter") or \
            mep_elem.get_Parameter(DB.BuiltInParameter.RBS_PIPE_DIAMETER_PARAM) or \
            mep_elem.get_Parameter(DB.BuiltInParameter.RBS_CURVE_DIAMETER_PARAM)

    if p_dia:
        dia = p_dia.AsDouble()
        return dia, dia, dia

    if p_w and p_h:
        return p_w.AsDouble(), p_h.AsDouble(), 0.0

    bb = mep_elem.get_BoundingBox(None)
    if bb:
        w, h = abs(bb.Max.X - bb.Min.X), abs(bb.Max.Y - bb.Min.Y)
        return w, h, 0.0

    return 0.0, 0.0, 0.0


def get_element_orientation_angle(elem):
    try:
        if isinstance(elem, DB.FamilyInstance):
            hand = elem.HandOrientation
            if hand and (abs(hand.X) > 1e-5 or abs(hand.Y) > 1e-5):
                return math.atan2(hand.Y, hand.X)
            if isinstance(elem.Location, DB.LocationPoint):
                return elem.Location.Rotation

        if hasattr(elem, "Location") and elem.Location and isinstance(elem.Location, DB.LocationCurve) and elem.Location.Curve:
            p0 = elem.Location.Curve.GetEndPoint(0)
            p1 = elem.Location.Curve.GetEndPoint(1)
            dx, dy = p1.X - p0.X, p1.Y - p0.Y
            if abs(dx) > 1e-5 or abs(dy) > 1e-5:
                return math.atan2(dy, dx)

        if hasattr(elem, "ConnectorManager") and elem.ConnectorManager:
            for conn in elem.ConnectorManager.Connectors:
                cs = conn.CoordinateSystem
                if cs and cs.BasisX:
                    bx = cs.BasisX
                    if abs(bx.X) > 1e-5 or abs(bx.Y) > 1e-5:
                        return math.atan2(bx.Y, bx.X)

        if hasattr(elem, "Location") and isinstance(elem.Location, DB.LocationPoint):
            return elem.Location.Rotation
    except Exception:
        pass
    return 0.0


def get_instance_hand_angle(inst):
    try:
        hand = inst.HandOrientation
        if hand and (abs(hand.X) > 1e-5 or abs(hand.Y) > 1e-5):
            return math.atan2(hand.Y, hand.X)
        if isinstance(inst.Location, DB.LocationPoint):
            return inst.Location.Rotation
    except Exception:
        pass
    return 0.0


def set_aligned_instance_rotation(inst, placement_pt, target_rotation_rad):
    try:
        current_angle = get_instance_hand_angle(inst)
        delta_angle = target_rotation_rad - current_angle
        delta_angle = (delta_angle + math.pi) % (2 * math.pi) - math.pi

        if abs(delta_angle) > 0.01:
            axis = DB.Line.CreateBound(
                placement_pt,
                DB.XYZ(placement_pt.X, placement_pt.Y, placement_pt.Z + 1.0)
            )
            DB.ElementTransformUtils.RotateElement(doc, inst.Id, axis, delta_angle)
            return True
    except Exception:
        pass
    return False


def get_closest_level(placement_z):
    levels = DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements()
    if not levels:
        return None
    return min(levels, key=lambda lvl: abs(lvl.Elevation - placement_z))


def get_mep_reference_level(elem, placement_z=None):
    if placement_z is not None:
        closest = get_closest_level(placement_z)
        if closest:
            return closest

    p_ref = elem.get_Parameter(DB.BuiltInParameter.RBS_START_LEVEL_PARAM)
    if p_ref and p_ref.AsElementId() != DB.ElementId.InvalidElementId:
        return doc.GetElement(p_ref.AsElementId())

    p_ref_named = elem.LookupParameter("Reference Level")
    if p_ref_named and p_ref_named.AsElementId() != DB.ElementId.InvalidElementId:
        return doc.GetElement(p_ref_named.AsElementId())

    if elem.LevelId != DB.ElementId.InvalidElementId:
        return doc.GetElement(elem.LevelId)

    if hasattr(doc.ActiveView, "GenLevel") and doc.ActiveView.GenLevel:
        return doc.ActiveView.GenLevel

    levels = DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements()
    return levels[0] if levels else None


def get_elevation_param(inst):
    for bip in [DB.BuiltInParameter.INSTANCE_ELEVATION_PARAM, DB.BuiltInParameter.INSTANCE_FREE_HOST_OFFSET_PARAM]:
        p = inst.get_Parameter(bip)
        if p and not p.IsReadOnly:
            return p
    for name in ["Elevation from Level", "Offset", "Elevation"]:
        p = inst.LookupParameter(name)
        if p and not p.IsReadOnly:
            return p
    return None


def set_elevation_from_level(inst, target_level, absolute_z):
    target_offset = absolute_z - target_level.Elevation
    p_elev = get_elevation_param(inst)
    if p_elev:
        p_elev.Set(target_offset)
        return

    if isinstance(inst.Location, DB.LocationPoint):
        cur_z = inst.Location.Point.Z
        dz = absolute_z - cur_z
        if abs(dz) > 0.001:
            DB.ElementTransformUtils.MoveElement(doc, inst.Id, DB.XYZ(0, 0, dz))


def apply_penetration_parameters(inst, target_barrier, target_w, target_h, target_dia):
    if target_barrier > 0:
        for b_name in ["BarrierThickness", "Barrier Thickness"]:
            p_b = inst.LookupParameter(b_name)
            if p_b and not p_b.IsReadOnly:
                p_b.Set(target_barrier)
                break

    params = {
        "Diameter": target_dia,
        "Width": target_w,
        "Height": target_h
    }
    for param_name, value in params.items():
        if value > 0:
            p = inst.LookupParameter(param_name)
            if p and not p.IsReadOnly:
                p.Set(value)


def find_existing_penetration(pt, radius=1.0):
    outline = DB.Outline(
        DB.XYZ(pt.X - radius, pt.Y - radius, pt.Z - radius),
        DB.XYZ(pt.X + radius, pt.Y + radius, pt.Z + radius)
    )
    existing_instances = DB.FilteredElementCollector(doc)\
                           .OfCategory(DB.BuiltInCategory.OST_GenericModel)\
                           .OfClass(DB.FamilyInstance)\
                           .WherePasses(DB.BoundingBoxIntersectsFilter(outline))\
                           .ToElements()

    for inst in existing_instances:
        if isinstance(inst.Location, DB.LocationPoint):
            dist_xy = math.hypot(inst.Location.Point.X - pt.X, inst.Location.Point.Y - pt.Y)
            dist_z = abs(inst.Location.Point.Z - pt.Z)
            if dist_xy <= radius and dist_z <= max(radius, 1.5):
                if inst.LookupParameter("BarrierThickness") is not None or inst.LookupParameter("Barrier Thickness") is not None:
                    return inst
    return None


def get_view_crop_outline(view):
    try:
        box = None
        if isinstance(view, DB.View3D) and view.IsSectionBoxActive:
            box = view.GetSectionBox()
        elif view.CropBoxActive:
            box = view.CropBox

        if box:
            trans = box.Transform
            p1, p2 = trans.OfPoint(box.Min), trans.OfPoint(box.Max)
            return DB.Outline(
                DB.XYZ(min(p1.X, p2.X), min(p1.Y, p2.Y), min(p1.Z, p2.Z)),
                DB.XYZ(max(p1.X, p2.X), max(p1.Y, p2.Y), max(p1.Z, p2.Z))
            )
    except Exception:
        pass
    return None


# --- MAIN EXECUTION ---
xaml_path = os.path.join(os.path.dirname(__file__), "UI.xaml")
win = PenetrationUI(xaml_path)
win.show_dialog()

if not win.mode:
    sys.exit()

target_host_doc = win.target_doc
link_inst = win.target_link_inst
category_bic = win.selected_category_bic
type_kw = win.type_filter_keyword
config = win.config
is_fire_wall = win.is_fire_wall

link_transform = link_inst.GetTotalTransform() if link_inst else DB.Transform.Identity

active_categories = []
if win.enable_mep:
    active_categories.extend(BASE_MEP_CATEGORIES)
if win.enable_air_terminal:
    active_categories.append(DB.BuiltInCategory.OST_DuctTerminal)
if win.enable_generic_model:
    active_categories.append(DB.BuiltInCategory.OST_GenericModel)

peno_symbol_ids = set()
for k in config:
    sym = config[k].get("symbol")
    if sym:
        peno_symbol_ids.add(sym.Id)

mep_elements = []

if win.mode == "PICK_ELEMENTS":
    try:
        refs = uidoc.Selection.PickObjects(
            Selection.ObjectType.Element,
            MEPSelectionFilter(active_categories, peno_symbol_ids),
            "Select target elements. Click FINISH in top-left options bar."
        )
        mep_elements = [doc.GetElement(r.ElementId) for r in refs]
    except Exception:
        sys.exit()

elif win.mode == "ENTIRE_VIEW":
    cat_filters = [DB.ElementCategoryFilter(c) for c in active_categories]
    multi_cat_filter = DB.LogicalOrFilter(cat_filters) if len(cat_filters) > 1 else cat_filters[0]

    raw_elements = list(
        DB.FilteredElementCollector(doc, doc.ActiveView.Id)
          .WherePasses(multi_cat_filter)
          .WhereElementIsNotElementType()
          .ToElements()
    )

    mep_elements = [
        el for el in raw_elements
        if not is_union_fitting(el) and not is_penetration_instance(el, peno_symbol_ids)
    ]

    if not mep_elements:
        forms.alert("No matching elements found in the Active View.", title="Warning", warn_icon=True)
        sys.exit()

    confirm_msg = (
        u"WARNING: You are about to generate/update penetrations for {} element(s) in Active View:\n"
        u"\u2022 Tool: Wall Penetration\n"
        u"\u2022 View Name: {}\n\n"
        u"Do you want to proceed?"
    ).format(len(mep_elements), doc.ActiveView.Name)

    user_choice = forms.alert(
        confirm_msg,
        title="Confirm Entire Active View",
        options=["Yes", "No"],
        warn_icon=True
    )
    if user_choice != "Yes":
        sys.exit()

if not mep_elements:
    forms.alert("No matching elements found.")
    sys.exit()

all_hosts = DB.FilteredElementCollector(target_host_doc)\
              .OfCategory(category_bic)\
              .WhereElementIsNotElementType()\
              .ToElements()

host_elements = [h for h in all_hosts if not type_kw or type_kw in get_element_type_name(h)]

if not host_elements:
    forms.alert("No wall elements matching filter were found.")
    sys.exit()

# Pre-cache wall solids and exact thicknesses
host_cache = []
for h in host_elements:
    h_solid = get_element_solid(h, link_transform)
    if h_solid:
        h_thick = get_wall_thickness(h)
        host_cache.append((h, h_solid, h_thick))

view_outline = get_view_crop_outline(doc.ActiveView)

placed_count = 0
updated_count = 0
skipped_count = 0

t = DB.Transaction(doc, "Generate/Update Wall Penetrations")
t.Start()

for profile_key in config:
    sym = config[profile_key].get("symbol")
    if sym and not sym.IsActive:
        sym.Activate()

for mep_elem in mep_elements:
    if is_union_fitting(mep_elem) or is_penetration_instance(mep_elem, peno_symbol_ids):
        continue

    profile_type = get_mep_profile_type(mep_elem)
    if profile_type == "AIR_TERMINAL" and not win.enable_air_terminal:
        continue
    if profile_type == "GENERIC_MODEL" and not win.enable_generic_model:
        continue
    if profile_type in ["RECT", "ROUND_DUCT", "PIPE"] and not win.enable_mep:
        continue

    mep_solid = get_element_solid(mep_elem)
    if not mep_solid:
        continue

    target_w, target_h, target_dia = 0.0, 0.0, 0.0
    ui_rotation_rad = 0.0

    if profile_type == "AIR_TERMINAL":
        at_w, at_h, at_dia, is_round_at = get_air_terminal_dimensions(mep_elem)
        if is_round_at:
            prof_cfg = config["AIR_TERMINAL_ROUND"]
            symbol = prof_cfg["symbol"]
            offset_feet = prof_cfg["offset_feet"]
            ui_rotation_rad = prof_cfg["rotation_rad"]
            target_dia = at_dia + offset_feet if at_dia > 0 else 0.0
            target_w = target_dia
            target_h = target_dia
        else:
            prof_cfg = config["AIR_TERMINAL_RECT"]
            symbol = prof_cfg["symbol"]
            offset_feet = prof_cfg["offset_feet"]
            ui_rotation_rad = prof_cfg["rotation_rad"]
            target_w = at_w + offset_feet if at_w > 0 else 0.0
            target_h = at_h + offset_feet if at_h > 0 else 0.0
            target_dia = 0.0

    elif profile_type == "GENERIC_MODEL":
        prof_cfg = config["GENERIC_MODEL"]
        symbol = prof_cfg["symbol"]
        offset_feet = prof_cfg["offset_feet"]
        ui_rotation_rad = prof_cfg["rotation_rad"]
        gm_w, gm_h, _ = get_generic_model_dimensions(mep_elem)
        target_w = gm_w + offset_feet if gm_w > 0 else 0.0
        target_h = gm_h + offset_feet if gm_h > 0 else 0.0
        target_dia = 0.0

    elif profile_type == "PIPE":
        prof_cfg = config["PIPE"]
        symbol = prof_cfg["symbol"]
        offset_feet = prof_cfg["offset_feet"]
        ui_rotation_rad = prof_cfg["rotation_rad"]
        if is_refrigerant_pipe(mep_elem):
            target_dia = mm_to_feet(150.0)
            target_w = target_dia
            target_h = target_dia
        else:
            _, _, mep_dia = get_mep_dimensions(mep_elem)
            insul_2x = get_insulation_thickness_2x(mep_elem)
            target_dia = mep_dia + insul_2x + offset_feet if mep_dia > 0 else 0.0
            target_w = target_dia
            target_h = target_dia

    elif profile_type == "ROUND_DUCT":
        prof_cfg = config["ROUND_DUCT"]
        symbol = prof_cfg["symbol"]
        offset_feet = prof_cfg["offset_feet"]
        ui_rotation_rad = prof_cfg["rotation_rad"]
        _, _, mep_dia = get_mep_dimensions(mep_elem)
        if is_fire_wall:
            dia_mm = feet_to_mm(mep_dia)
            fire_offset_feet = mm_to_feet(25.0) if dia_mm < 1500.0 else mm_to_feet(50.0)
            target_dia = mep_dia + fire_offset_feet if mep_dia > 0 else 0.0
        else:
            insul_2x = get_insulation_thickness_2x(mep_elem)
            target_dia = mep_dia + insul_2x + offset_feet if mep_dia > 0 else 0.0
        target_w = target_dia
        target_h = target_dia

    else:  # RECT (Rectangular Ducts & Rectangular Duct Accessories)
        prof_cfg = config["RECT"]
        symbol = prof_cfg["symbol"]
        offset_feet = prof_cfg["offset_feet"]
        ui_rotation_rad = prof_cfg["rotation_rad"]
        mep_w, mep_h, _ = get_mep_dimensions(mep_elem)
        if is_fire_wall:
            max_dim_mm = feet_to_mm(max(mep_w, mep_h))
            fire_offset_feet = mm_to_feet(25.0) if max_dim_mm < 1500.0 else mm_to_feet(50.0)
            target_w = mep_w + fire_offset_feet if mep_w > 0 else 0.0
            target_h = mep_h + fire_offset_feet if mep_h > 0 else 0.0
            target_dia = 0.0
        else:
            insul_2x = get_insulation_thickness_2x(mep_elem)
            target_w = mep_w + insul_2x + offset_feet if mep_w > 0 else 0.0
            target_h = mep_h + insul_2x + offset_feet if mep_h > 0 else 0.0
            target_dia = 0.0

    elem_angle_rad = get_element_orientation_angle(mep_elem)
    target_rotation_rad = elem_angle_rad + ui_rotation_rad

    for host_elem, host_solid, target_barrier in host_cache:
        placement_pt = None
        try:
            intersection_solid = DB.BooleanOperationsUtils.ExecuteBooleanOperation(
                mep_solid, host_solid, DB.BooleanOperationsType.Intersect
            )
            if intersection_solid and intersection_solid.Volume > 0.00001:
                placement_pt = intersection_solid.ComputeCentroid()
        except Exception:
            pass

        if placement_pt:
            if view_outline and not view_outline.Contains(placement_pt, 0.01):
                continue

            target_z = placement_pt.Z
            target_level = get_mep_reference_level(mep_elem, target_z)
            if not target_level:
                continue

            existing_inst = find_existing_penetration(placement_pt, radius=1.0)

            if existing_inst:
                p_barrier = existing_inst.LookupParameter("BarrierThickness") or existing_inst.LookupParameter("Barrier Thickness")
                p_dia = existing_inst.LookupParameter("Diameter")
                p_w = existing_inst.LookupParameter("Width")
                p_h = existing_inst.LookupParameter("Height")
                p_lvl = existing_inst.get_Parameter(DB.BuiltInParameter.FAMILY_LEVEL_PARAM)

                cur_barrier = p_barrier.AsDouble() if p_barrier else 0.0
                cur_dia = p_dia.AsDouble() if p_dia else 0.0
                cur_w = p_w.AsDouble() if p_w else 0.0
                cur_h = p_h.AsDouble() if p_h else 0.0
                cur_symbol_id = existing_inst.Symbol.Id
                cur_level_id = p_lvl.AsElementId() if p_lvl else DB.ElementId.InvalidElementId

                type_changed = (cur_symbol_id != symbol.Id)
                level_changed = (cur_level_id != target_level.Id)
                barrier_changed = abs(cur_barrier - target_barrier) > 0.001
                dia_changed = abs(cur_dia - target_dia) > 0.001 if target_dia > 0 else False
                width_changed = abs(cur_w - target_w) > 0.001 if target_w > 0 else False
                height_changed = abs(cur_h - target_h) > 0.001 if target_h > 0 else False

                rotation_changed = set_aligned_instance_rotation(existing_inst, placement_pt, target_rotation_rad)

                if type_changed:
                    existing_inst.Symbol = symbol
                if p_lvl and not p_lvl.IsReadOnly and level_changed:
                    p_lvl.Set(target_level.Id)

                set_elevation_from_level(existing_inst, target_level, target_z)
                apply_penetration_parameters(existing_inst, target_barrier, target_w, target_h, target_dia)

                if type_changed or level_changed or barrier_changed or dia_changed or width_changed or height_changed or rotation_changed:
                    updated_count += 1
                else:
                    skipped_count += 1

            else:
                place_xyz = DB.XYZ(placement_pt.X, placement_pt.Y, target_z)
                inst = doc.Create.NewFamilyInstance(
                    place_xyz,
                    symbol,
                    target_level,
                    DB.Structure.StructuralType.NonStructural
                )

                set_elevation_from_level(inst, target_level, target_z)
                set_aligned_instance_rotation(inst, place_xyz, target_rotation_rad)
                apply_penetration_parameters(inst, target_barrier, target_w, target_h, target_dia)

                placed_count += 1

t.Commit()

forms.alert(
    "Wall Penetration Process Completed:\n\n"
    "- Newly Placed: {}\n"
    "- Updated: {}\n"
    "- Unchanged (Skipped): {}".format(placed_count, updated_count, skipped_count)
)