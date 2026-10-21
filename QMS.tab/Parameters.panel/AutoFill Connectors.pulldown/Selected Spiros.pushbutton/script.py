# -*- coding: utf-8 -*-
from pyrevit import forms, revit
from Autodesk.Revit.DB import BuiltInParameter, Transaction
from Autodesk.Revit.DB.Mechanical import Duct

doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument

TARGET_FAMILY_KEYWORD = "Round Duct"
JOINER_KEYWORD = "Round_Joiner_QMS"
PARAM_C1 = "ConnectorC1Type"
PARAM_C2 = "ConnectorC2Type"

# 1. Get current selection or prompt user to pick elements
selection = [doc.GetElement(el_id) for el_id in uidoc.Selection.GetElementIds()]

if not selection:
    try:
        selection = revit.pick_elements(xmsg="Select Round Ducts to AutoFill")
    except Exception:
        selection = []

target_ducts = []
processed_types = set()

for e in selection:
    if isinstance(e, Duct):
        duct_type = doc.GetElement(e.GetTypeId())
        if duct_type:
            fam_param = duct_type.get_Parameter(BuiltInParameter.ALL_MODEL_FAMILY_NAME)
            fam_name = fam_param.AsString() if fam_param else ""
            if not fam_name and hasattr(duct_type, "FamilyName"):
                fam_name = duct_type.FamilyName
                
            if TARGET_FAMILY_KEYWORD.lower() in fam_name.lower():
                target_ducts.append(e)
                type_param = duct_type.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
                if type_param and type_param.AsString():
                    processed_types.add(type_param.AsString())

# 2. Validation & Execution
if not target_ducts:
    forms.alert("No valid round ducts selected.", title="Warning", warn_icon=True)
else:
    sample_duct = target_ducts[0]
    if not sample_duct.LookupParameter(PARAM_C1) or not sample_duct.LookupParameter(PARAM_C2):
        forms.alert("Project is missing required Shared Parameters!", title="Error", warn_icon=True)
    else:
        processed_joiners = set()
        updated_count = 0
        joiner_count = 0

        t = Transaction(doc, "AutoFill Connector Types (Selection)")
        t.Start()
        for duct in target_ducts:
            conn_mgr = duct.ConnectorManager
            if not conn_mgr:
                continue
                
            connectors = sorted([c for c in conn_mgr.Connectors], key=lambda c: c.Id)
            val_c1, val_c2 = "RE", "RE"
            
            if len(connectors) >= 2:
                c1, c2 = connectors[0], connectors[1]
                
                def get_connected_info(connector, current_duct_id):
                    for ref in connector.AllRefs:
                        if ref.Owner.Id != current_duct_id and str(ref.ConnectorType) == "End":
                            owner = ref.Owner
                            f_name = ""
                            if hasattr(owner, "Symbol") and owner.Symbol:
                                f_name = owner.Symbol.Family.Name
                            elif hasattr(owner, "Name"):
                                f_name = owner.Name
                            return f_name, owner.Id
                    return "", None

                c1_fam, c1_id = get_connected_info(c1, duct.Id)
                c2_fam, c2_id = get_connected_info(c2, duct.Id)

                if JOINER_KEYWORD.lower() in c1_fam.lower() and c1_id not in processed_joiners:
                    val_c1, val_c2 = "Joiner", "RE"
                    processed_joiners.add(c1_id)
                    joiner_count += 1
                elif JOINER_KEYWORD.lower() in c2_fam.lower() and c2_id not in processed_joiners:
                    val_c1, val_c2 = "RE", "Joiner"
                    processed_joiners.add(c2_id)
                    joiner_count += 1
                else:
                    val_c1, val_c2 = "RE", "RE"

            p1 = duct.LookupParameter(PARAM_C1)
            p2 = duct.LookupParameter(PARAM_C2)
            
            if p1 and not p1.IsReadOnly: p1.Set(val_c1)
            if p2 and not p2.IsReadOnly: p2.Set(val_c2)
            updated_count += 1
        t.Commit()

        types_str = ", ".join(sorted(list(processed_types)))
        forms.alert(
            "SUCCESS!\n\n"
            "- Mode: Manual Selection\n"
            "- Ducts Updated: {}\n"
            "- Joiners Processed: {}\n"
            "- Family Types: {}".format(updated_count, joiner_count, types_str),
            title="AutoFill Complete",
            warn_icon=False
        )