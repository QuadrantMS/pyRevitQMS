from pyrevit import revit, DB

# Get active view
view = revit.active_view

# Get selected elements (these are Elements, not ElementIds)
selected_elements = revit.get_selection()

if not selected_elements:
    print "Please select one or more elements."
else:
    # Grey color RGB 128,128,128
    grey = DB.Color(128, 128, 128)

    # Override settings
    ogs = DB.OverrideGraphicSettings()
    ogs.SetProjectionLineColor(grey)

    # Apply override
    with revit.Transaction("Override to Grey"):
        for elem in selected_elements:
            view.SetElementOverrides(elem.Id, ogs)