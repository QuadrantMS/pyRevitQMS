# -*- coding: utf-8 -*-
import sys
import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

from pyrevit import revit, DB, forms

doc = revit.doc
uidoc = revit.uidoc


# --- STEP 1: SELECT LINKED MODEL ---
model_options = {}

try:
    links = DB.FilteredElementCollector(doc)\
              .OfClass(DB.RevitLinkInstance)\
              .ToElements()

    for l in links:
        try:
            l_doc = l.GetLinkDocument()
            if not l_doc:
                continue

            raw_name = l.GetName() if hasattr(l, "GetName") else l_doc.Title
            clean_name = raw_name.split(" : ")[0]
            display_name = "[LINK] " + clean_name

            counter = 1
            base_display = display_name
            while display_name in model_options:
                display_name = "{} ({})".format(base_display, counter)
                counter += 1

            model_options[display_name] = (l_doc, l)
        except Exception:
            pass
except Exception:
    pass

if not model_options:
    forms.alert("No loaded linked models found in the active project.", exitscript=True)

selected_model_name = forms.SelectFromList.show(
    sorted(model_options.keys()),
    title="Step 1: Select Linked Model to Copy From",
    button_name="Next"
)

if not selected_model_name:
    sys.exit()

source_doc, link_instance = model_options[selected_model_name]


# --- STEP 2: SELECT TARGET GRID TYPE ---
grid_types = DB.FilteredElementCollector(doc)\
               .OfClass(DB.GridType)\
               .ToElements()

if not grid_types:
    forms.alert("No Grid Types found in current host project.", exitscript=True)

grid_type_map = {
    "{} : {}".format(gt.get_Parameter(DB.BuiltInParameter.ALL_MODEL_FAMILY_NAME).AsString(), DB.Element.Name.__get__(gt)): gt 
    for gt in grid_types
}

selected_grid_type_name = forms.SelectFromList.show(
    sorted(grid_type_map.keys()),
    title="Step 2: Select Grid Type for Host Model",
    button_name="Next",
    multiselect=False
)

if not selected_grid_type_name:
    sys.exit()

target_grid_type = grid_type_map[selected_grid_type_name]


# --- STEP 3: SELECT TARGET LEVEL TYPE ---
level_types = DB.FilteredElementCollector(doc)\
                .OfClass(DB.LevelType)\
                .ToElements()

if not level_types:
    forms.alert("No Level Types found in current host project.", exitscript=True)

level_type_map = {
    "{} : {}".format(lt.get_Parameter(DB.BuiltInParameter.ALL_MODEL_FAMILY_NAME).AsString(), DB.Element.Name.__get__(lt)): lt 
    for lt in level_types
}

selected_level_type_name = forms.SelectFromList.show(
    sorted(level_type_map.keys()),
    title="Step 3: Select Level Type for Host Model",
    button_name="Copy Elements",
    multiselect=False
)

if not selected_level_type_name:
    sys.exit()

target_level_type = level_type_map[selected_level_type_name]


# --- STEP 4: COLLECT SOURCE GRIDS & LEVELS ---
source_grids = DB.FilteredElementCollector(source_doc)\
                 .OfCategory(DB.BuiltInCategory.OST_Grids)\
                 .WhereElementIsNotElementType()\
                 .ToElements()

source_levels = DB.FilteredElementCollector(source_doc)\
                  .OfCategory(DB.BuiltInCategory.OST_Levels)\
                  .WhereElementIsNotElementType()\
                  .ToElements()

if not source_grids and not source_levels:
    forms.alert("No Grids or Levels found in the selected linked model.", exitscript=True)

# Collect Elevation/Section Views
host_elev_views = [
    v for v in DB.FilteredElementCollector(doc).OfClass(DB.View).ToElements()
    if not v.IsTemplate and v.ViewType in [DB.ViewType.Elevation, DB.ViewType.Section]
]

source_elev_views = [
    v for v in DB.FilteredElementCollector(source_doc).OfClass(DB.View).ToElements()
    if not v.IsTemplate and v.ViewType in [DB.ViewType.Elevation, DB.ViewType.Section]
]

# Collect Host Scope Boxes (if applicable)
host_scope_boxes = {
    sb.Name: sb 
    for sb in DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_VolumeOfInterest).ToElements()
}


# --- STEP 5: EXECUTE COPYING WITH PROJECTED LEVEL EXTENTS ---
link_transform = link_instance.GetTotalTransform() if link_instance else DB.Transform.Identity

copied_grids_count = 0
copied_levels_count = 0

existing_levels = {
    lvl.Name: lvl 
    for lvl in DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Levels).WhereElementIsNotElementType().ToElements()
}

existing_grids = {
    grd.Name: grd 
    for grd in DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Grids).WhereElementIsNotElementType().ToElements()
}

t = DB.Transaction(doc, "Copy Grids & Levels from Link")
t.Start()

# 1. PROCESS LEVELS
for src_level in source_levels:
    try:
        transformed_elev = src_level.Elevation + link_transform.Origin.Z

        if src_level.Name in existing_levels:
            new_level = existing_levels[src_level.Name]
            new_level.Elevation = transformed_elev
        else:
            new_level = DB.Level.Create(doc, transformed_elev)
            try:
                new_level.Name = src_level.Name
            except Exception:
                pass

        new_level.ChangeTypeId(target_level_type.Id)

        # Check if source level uses a Scope Box and apply matching host Scope Box
        p_src_scope = src_level.get_Parameter(DB.BuiltInParameter.VIEWER_VOLUME_OF_INTEREST_CROP)
        scope_assigned = False
        if p_src_scope and p_src_scope.AsElementId() != DB.ElementId.InvalidElementId:
            src_scope_box = source_doc.GetElement(p_src_scope.AsElementId())
            if src_scope_box and src_scope_box.Name in host_scope_boxes:
                p_host_scope = new_level.get_Parameter(DB.BuiltInParameter.VIEWER_VOLUME_OF_INTEREST_CROP)
                if p_host_scope and not p_host_scope.IsReadOnly:
                    p_host_scope.Set(host_scope_boxes[src_scope_box.Name].Id)
                    scope_assigned = True

        # If no scope box assigned, project 3D extents onto host elevation view planes
        if not scope_assigned and host_elev_views:
            for h_view in host_elev_views:
                try:
                    h_dir = h_view.ViewDirection
                    h_right = h_view.RightDirection
                    h_origin = h_view.Origin

                    # Find matching direction view in linked document
                    matching_s_view = None
                    for s_view in source_elev_views:
                        try:
                            s_dir_transformed = link_transform.OfVector(s_view.ViewDirection)
                            if abs(s_dir_transformed.DotProduct(h_dir)) > 0.9:
                                matching_s_view = s_view
                                break
                        except Exception:
                            pass

                    if not matching_s_view:
                        # Fallback to any source view that contains curves
                        for s_view in source_elev_views:
                            try:
                                curves = src_level.GetCurvesInView(DB.DatumExtentType.Model, s_view)
                                if curves and len(curves) > 0:
                                    matching_s_view = s_view
                                    break
                            except Exception:
                                pass

                    if not matching_s_view:
                        continue

                    src_curves = src_level.GetCurvesInView(DB.DatumExtentType.Model, matching_s_view)
                    if not src_curves or len(src_curves) == 0:
                        continue

                    src_line = src_curves[0]
                    p1 = link_transform.OfPoint(src_line.GetEndPoint(0))
                    p2 = link_transform.OfPoint(src_line.GetEndPoint(1))

                    # Project 3D points onto host view plane along RightDirection
                    d1 = (p1 - h_origin).DotProduct(h_right)
                    d2 = (p2 - h_origin).DotProduct(h_right)

                    if abs(d1 - d2) < 0.1:
                        continue

                    q1 = DB.XYZ(h_origin.X + d1 * h_right.X, h_origin.Y + d1 * h_right.Y, transformed_elev)
                    q2 = DB.XYZ(h_origin.X + d2 * h_right.X, h_origin.Y + d2 * h_right.Y, transformed_elev)

                    proj_line = DB.Line.CreateBound(q1, q2)
                    new_level.SetCurveInView(DB.DatumExtentType.Model, h_view, proj_line)
                except Exception:
                    pass

        copied_levels_count += 1
    except Exception:
        pass


# 2. PROCESS GRIDS
for src_grid in source_grids:
    try:
        src_curve = src_grid.Curve
        if not src_curve:
            continue

        transformed_curve = src_curve.CreateTransformed(link_transform)

        if src_grid.Name in existing_grids:
            new_grid = existing_grids[src_grid.Name]
        else:
            new_grid = DB.Grid.Create(doc, transformed_curve)
            try:
                new_grid.Name = src_grid.Name
            except Exception:
                pass

        new_grid.ChangeTypeId(target_grid_type.Id)

        try:
            if doc.ActiveView.ViewType in [DB.ViewType.FloorPlan, DB.ViewType.CeilingPlan, DB.ViewType.EngineeringPlan]:
                new_grid.SetCurveInView(DB.DatumExtentType.Model, doc.ActiveView, transformed_curve)
        except Exception:
            pass

        copied_grids_count += 1
    except Exception:
        pass

t.Commit()


# --- NOTIFICATION ---
forms.alert(
    "Grid & Level Copy Complete!\n\n"
    "- Grids Processed: {}\n"
    "- Levels Processed (3D Extents Matched): {}".format(copied_grids_count, copied_levels_count),
    title="Copy Complete"
)