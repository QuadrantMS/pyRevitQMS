# coding: utf8

from Autodesk.Revit import Exceptions
from Autodesk.Revit.DB import (
    InsulationLiningBase,
    Document,
    FamilyInstance,
)
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType

from pyrevit import script, forms, revit

__doc__ = """Create a MEP transition between 2 open ends with custom length override.
Select first object (pick a location close to the desired connector)
Select second object (pick a location close to the desired connector)
Includes options to Accept, Redraw, or Flipped Redraw the newly created transition fitting."""
__title__ = "Transition"
__author__ = "Cyril Waechter / Customized"

logger = script.get_logger()
uidoc = __revit__.ActiveUIDocument
doc = __revit__.ActiveUIDocument.Document  # type: Document


# --- UNIT CONVERSION HELPER ---

def mm_to_feet(mm_val):
    """Converts millimeters to internal Revit feet."""
    return float(mm_val) / 304.8


# --- EMBEDDED CONNECTOR HELPERS ---

def get_connector_manager(elem):
    """Safely retrieves ConnectorManager from Curve or FamilyInstance."""
    if hasattr(elem, "ConnectorManager") and elem.ConnectorManager:
        return elem.ConnectorManager
    elif hasattr(elem, "MEPModel") and elem.MEPModel and elem.MEPModel.ConnectorManager:
        return elem.MEPModel.ConnectorManager
    return None


def get_unused_connectors(elem):
    """Returns a list of unused/open connectors for an element."""
    cm = get_connector_manager(elem)
    if not cm:
        return []
    unused = []
    for conn in cm.Connectors:
        if not conn.IsConnected:
            unused.append(conn)
    return unused


def get_connector_closest_to(connectors, pt):
    """Finds the connector closest to a given XYZ point."""
    if not connectors or not pt:
        return None
    closest_conn = None
    min_dist = float("inf")
    for conn in connectors:
        dist = conn.Origin.DistanceTo(pt)
        if dist < min_dist:
            min_dist = dist
            closest_conn = conn
    return closest_conn


# --- SELECTION FILTERS ---

class NoInsulation(ISelectionFilter):
    def AllowElement(self, elem):
        if isinstance(elem, InsulationLiningBase):
            return False
        return get_connector_manager(elem) is not None

    def AllowReference(self, reference, position):
        return True


class NoInsulationNoFamilyInstance(ISelectionFilter):
    def AllowElement(self, elem):
        if isinstance(elem, InsulationLiningBase) or isinstance(elem, FamilyInstance):
            return False
        return get_connector_manager(elem) is not None

    def AllowReference(self, reference, position):
        return True


# --- HELPER TO BUILD TRANSITION FITTING ---

def create_and_setup_transition(element1, xyz1, element2, xyz2, is_flipped, length_feet):
    """Retrieves connectors and executes the transaction to place the transition."""
    connector1 = get_connector_closest_to(
        get_unused_connectors(element1), xyz1
    )
    connector2 = get_connector_closest_to(
        get_unused_connectors(element2), xyz2
    )

    if not connector1 or not connector2:
        forms.alert(
            "It looks like one of the objects has no unused connector.",
            title="Connector Error",
        )
        return None

    try:
        if connector1.Domain != connector2.Domain:
            forms.alert(
                "You picked 2 connectors of different domains. Please retry.",
                title="Domain Error",
            )
            return None
    except AttributeError:
        forms.alert(
            "It looks like one of the objects has no unused connector.",
            title="AttributeError",
        )
        return None

    created_fitting = None
    t_title = "Create Flipped Transition" if is_flipped else "Create Transition"
    
    with revit.Transaction(t_title):
        try:
            if is_flipped:
                # Reverses connector order to flip Connector 1 and Connector 2
                created_fitting = doc.Create.NewTransitionFitting(connector2, connector1)
            else:
                created_fitting = doc.Create.NewTransitionFitting(connector1, connector2)

            if created_fitting:
                p_override = created_fitting.LookupParameter("DuctLengthOverride")
                if p_override and not p_override.IsReadOnly:
                    p_override.Set(length_feet)
                else:
                    forms.alert(
                        "Parameter 'DuctLengthOverride' was not found or is read-only on this transition family.",
                        title="Parameter Warning"
                    )

        except Exceptions.InvalidOperationException:
            cmd_link1_msg = "Show example image"
            result = forms.alert(
                "Unable to connect.",
                sub_msg="Make sure you click near connectors you want to make a transition between.",
                options=[cmd_link1_msg]
            )
            if result == cmd_link1_msg:
                import os
                os.startfile(os.path.join(__commandpath__, "WhereToClick.png"))
            return None

    return created_fitting


# --- MAIN TRANSITION FUNCTION ---

def new_transition():
    # Standard placement mode by default
    is_flipped = False

    # 1. Prompt user to select elements and points to connect directly
    try:
        message = "Pick element 1 (moving)"
        with forms.WarningBar(title=message):
            reference = uidoc.Selection.PickObject(
                ObjectType.Element, NoInsulationNoFamilyInstance(), message
            )
    except Exceptions.OperationCanceledException:
        return False

    try:
        element1 = doc.GetElement(reference)
        xyz1 = reference.GlobalPoint

        message = "Pick element 2 (static)"
        with forms.WarningBar(title=message):
            reference = uidoc.Selection.PickObject(
                ObjectType.Element, NoInsulation(), message
            )
        element2 = doc.GetElement(reference)
        xyz2 = reference.GlobalPoint
    except Exceptions.OperationCanceledException:
        return True

    # 2. Prompt user for Total Length Override input
    length_str = forms.ask_for_string(
        default='300',
        prompt='Enter Total Transition Length in mm (DuctLengthOverride):',
        title='Transition Length Override'
    )

    if not length_str:
        return True

    try:
        length_mm = float(length_str)
        length_feet = mm_to_feet(length_mm)
    except ValueError:
        forms.alert("Invalid number entered for transition length.", title="Value Error")
        return True

    # 3 & 4. Create Transition Fitting
    created_fitting = create_and_setup_transition(
        element1, xyz1, element2, xyz2, is_flipped, length_feet
    )

    if not created_fitting:
        return True

    # 5. Post-placement options: Accept, Redraw, or Flipped Redraw
    opt_accept = "Accept Transition"
    opt_redraw = "Re Draw Transition"
    opt_flipped_redraw = "Flipped Redraw"

    while created_fitting:
        confirm = forms.alert(
            "Do you want to accept this transition, redraw, or flipped redraw?",
            options=[opt_accept, opt_redraw, opt_flipped_redraw],
            cancel=False
        )

        if confirm == opt_redraw:
            # Delete existing fitting and restart standard picker loop
            with revit.Transaction("Remove Transition"):
                doc.Delete(created_fitting.Id)
            return True

        elif confirm == opt_flipped_redraw:
            # Delete current transition fitting
            with revit.Transaction("Remove Transition"):
                doc.Delete(created_fitting.Id)

            # Toggle the flipped flag to reverse connector assignment order
            is_flipped = not is_flipped

            # Re-create transition using exact previous elements, points, and length
            created_fitting = create_and_setup_transition(
                element1, xyz1, element2, xyz2, is_flipped, length_feet
            )

        else:
            # User accepted transition, exit script loop
            return False

    return True


while new_transition():
    pass