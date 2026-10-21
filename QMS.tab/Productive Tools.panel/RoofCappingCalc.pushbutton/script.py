# -*- coding: utf-8 -*-
import sys
import os
import clr
from datetime import datetime

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')

from pyrevit import revit, DB, forms, script
from Autodesk.Revit.UI import Selection

doc = revit.doc
uidoc = revit.uidoc

# Initialize pyRevit persistent configuration object
cfg = script.get_config("DuctFrameCappingSettings")

class DuctSelectionFilter(Selection.ISelectionFilter):
    def AllowElement(self, elem):
        if elem and elem.Category:
            cat_id = elem.Category.Id.IntegerValue
            if cat_id == int(DB.BuiltInCategory.OST_DuctCurves):
                # Reject the element if it has a Diameter parameter (identifying it as round)[cite: 1]
                p_dia = elem.LookupParameter("Diameter") or elem.get_Parameter(DB.BuiltInParameter.RBS_CURVE_DIAMETER_PARAM)
                if p_dia:
                    return False
                return True
        return False
        
    def AllowReference(self, reference, point):
        return True

def feet_to_mm(feet_val):
    try:
        if hasattr(DB, "UnitUtils") and hasattr(DB, "UnitTypeId"):
            return DB.UnitUtils.ConvertFromInternalUnits(feet_val, DB.UnitTypeId.Millimeters)
        elif hasattr(DB, "DisplayUnitType"):
            return DB.UnitUtils.ConvertFromInternalUnits(feet_val, DB.DisplayUnitType.DUT_MILLIMETERS)
    except Exception:
        pass
    return float(feet_val) * 304.8


class CappingCalculatorUI(forms.WPFWindow):
    def __init__(self, xaml_file_name):
        forms.WPFWindow.__init__(self, xaml_file_name)
        
        self.action = None
        self.history_records = []
        self.history_data = []
        self.total_calcs = 0

        # Dynamic Event Bindings
        self.btn_SelectDuct.Click += self.OnSelectDuct
        self.btn_Calculate.Click += self.OnCalculate
        self.btn_Sum.Click += self.OnSumTotals
        self.btn_Clear.Click += self.OnClearHistory
        self.btn_Close.Click += self.OnClose

        self.load_history_from_config()
        self.load_inputs_from_config()

    def load_inputs_from_config(self):
        """ Restore the text box states so they persist when window reloads """
        self.tb_Width.Text = cfg.get_option("input_width", "500")
        self.tb_Height.Text = cfg.get_option("input_height", "300")
        self.tb_FrameSize.Text = cfg.get_option("input_frame", "35")
        self.tb_CountHoriz.Text = cfg.get_option("input_horiz", "1")
        self.tb_CountVert.Text = cfg.get_option("input_vert", "1")

    def save_inputs_to_config(self):
        """ Save current text box state before closing the window """
        cfg.set_option("input_width", self.tb_Width.Text)
        cfg.set_option("input_height", self.tb_Height.Text)
        cfg.set_option("input_frame", self.tb_FrameSize.Text)
        cfg.set_option("input_horiz", self.tb_CountHoriz.Text)
        cfg.set_option("input_vert", self.tb_CountVert.Text)
        script.save_config()

    def OnSelectDuct(self, sender, args):
        """ Safely closes the window to yield control to Revit for picking """
        self.save_inputs_to_config()
        self.action = "SELECT_DUCT"
        self.Close()

    def load_history_from_config(self):
        try:
            saved_records = cfg.get_option("history_records", None)
            saved_data = cfg.get_option("history_data", None)
            saved_total_calcs = cfg.get_option("total_calcs", 0)

            if saved_records and isinstance(saved_records, list):
                self.history_records = saved_records
            
            if saved_data and isinstance(saved_data, list):
                self.history_data = []
                for item in saved_data:
                    self.history_data.append({
                        'calc_id': int(item.get('calc_id', 0)),
                        'horiz_length': float(item.get('horiz_length', 0.0)),
                        'vert_length': float(item.get('vert_length', 0.0)),
                        'overall_length': float(item.get('overall_length', 0.0))
                    })

            self.total_calcs = int(saved_total_calcs)
            self.update_history_display()
        except Exception:
            pass

    def save_history_to_config(self):
        try:
            cfg.set_option("history_records", self.history_records)
            cfg.set_option("history_data", self.history_data)
            cfg.set_option("total_calcs", self.total_calcs)
            script.save_config()
        except Exception:
            pass

    def OnCalculate(self, sender, args):
        self.save_inputs_to_config()
        
        try: width = float(self.tb_Width.Text)
        except ValueError: return forms.alert("Invalid Duct Width.")
        
        try: height = float(self.tb_Height.Text)
        except ValueError: return forms.alert("Invalid Duct Height.")
        
        try: frame_size = float(self.tb_FrameSize.Text)
        except ValueError: return forms.alert("Invalid Frame Size.")
        
        try: count_horiz = int(self.tb_CountHoriz.Text)
        except ValueError: return forms.alert("Invalid Horizontal Count.")
        
        try: count_vert = int(self.tb_CountVert.Text)
        except ValueError: return forms.alert("Invalid Vertical Count.")

        if any(v <= 0 for v in [width, height, frame_size]) or count_horiz < 0 or count_vert < 0:
            return forms.alert("Dimensions must be > 0 mm. Counts must be >= 0.")

        single_horiz_len = (width + (frame_size * 2)) + ((height + (frame_size * 2)) * 2)
        single_vert_len = (2 * (width + (frame_size * 2))) + (2 * (height + (frame_size * 2)))

        total_horiz = single_horiz_len * count_horiz
        total_vert = single_vert_len * count_vert
        overall_total = total_horiz + total_vert

        self.total_calcs += 1
        self.history_data.append({
            'calc_id': self.total_calcs, 'horiz_length': total_horiz,
            'vert_length': total_vert, 'overall_length': overall_total
        })

        time_str = datetime.now().strftime("%d/%m %H:%M")
        entry = (
            "#{:d} [{}] Duct: {}x{}mm | Frame: {}mm\n"
            "  • Horiz Total (Qty: {}): {:,.2f} mm ({:.2f} m)\n"
            "  • Vert Total  (Qty: {}): {:,.2f} mm ({:.2f} m)\n"
            "  ----------------------------------------\n"
            "  • OVERALL TOTAL: {:,.2f} mm ({:.2f} m)"
        ).format(
            self.total_calcs, time_str, int(width), int(height), int(frame_size),
            count_horiz, total_horiz, total_horiz / 1000.0,
            count_vert, total_vert, total_vert / 1000.0,
            overall_total, overall_total / 1000.0
        )

        self.history_records.insert(0, entry)
        self.save_history_to_config()
        self.update_history_display()

    def OnSumTotals(self, sender, args):
        if not self.history_data:
            return forms.alert("No calculation history available to sum.")

        sum_horiz = sum(item['horiz_length'] for item in self.history_data)
        sum_vert = sum(item['vert_length'] for item in self.history_data)
        sum_overall = sum(item['overall_length'] for item in self.history_data)

        sum_msg = (
            "Cumulative Summary ({} Calculations):\n\n"
            "• Combined Horizontal: {:,.2f} mm ({:.2f} m)\n\n"
            "• Combined Vertical: {:,.2f} mm ({:.2f} m)\n\n"
            "========================================\n"
            "• GRAND OVERALL: {:,.2f} mm ({:.2f} m)"
        ).format(len(self.history_data), sum_horiz, sum_horiz / 1000.0, sum_vert, sum_vert / 1000.0, sum_overall, sum_overall / 1000.0)

        forms.alert(sum_msg, title="Cumulative Totals", ok=True)

    def OnClearHistory(self, sender, args):
        self.history_records = []
        self.history_data = []
        self.total_calcs = 0
        self.save_history_to_config()
        self.tb_History.Text = "No calculations run yet..."

    def update_history_display(self):
        if not self.history_records:
            self.tb_History.Text = "No calculations run yet..."
        else:
            self.tb_History.Text = "\n\n".join(self.history_records)

    def OnClose(self, sender, args):
        self.action = None
        self.Close()


# --- MODAL EXECUTION LOOP ---
xaml_path = os.path.join(os.path.dirname(__file__), "ui.xaml")

while True:
    win = CappingCalculatorUI(xaml_path)
    win.show_dialog()
    
    if win.action == "SELECT_DUCT":
        # Window is fully closed here, safe to run Revit picking tasks
        try:
            ref = uidoc.Selection.PickObject(
                Selection.ObjectType.Element,
                DuctSelectionFilter(),
                "Select a Rectangular Duct to extract Width and Height"
            )
            
            if ref:
                duct_elem = doc.GetElement(ref.ElementId)
                if duct_elem:
                    p_w = duct_elem.LookupParameter("Width") or duct_elem.get_Parameter(DB.BuiltInParameter.RBS_CURVE_WIDTH_PARAM)
                    p_h = duct_elem.LookupParameter("Height") or duct_elem.get_Parameter(DB.BuiltInParameter.RBS_CURVE_HEIGHT_PARAM)

                    if p_w and p_h:
                        w_mm = str(int(round(feet_to_mm(p_w.AsDouble()))))
                        h_mm = str(int(round(feet_to_mm(p_h.AsDouble()))))
                        cfg.set_option("input_width", w_mm)
                        cfg.set_option("input_height", h_mm)
                        script.save_config()
                    else:
                        forms.alert("Could not extract Width/Height parameters from selected duct.")
        except Exception:
            pass # User cancelled selection via Escape key
            
        # The while loop iterates and re-opens the window automatically
    else:
        # Window closed manually via 'X' or CLOSE button
        break