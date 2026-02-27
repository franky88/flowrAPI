from finance.models import WorkspaceMember, WorkspaceRole

def get_user_workspace(user_id: str, workspace_id: str, require_role: str = None):
    """
    Returns the WorkspaceMember if the user belongs to the workspace.
    Raises PermissionError if not a member or role insufficient.
    """
    try:
        member = WorkspaceMember.objects.select_related("workspace").get(
            user_id=user_id,
            workspace_id=workspace_id,
        )
    except WorkspaceMember.DoesNotExist:
        raise PermissionError("Not a member of this workspace.")

    if require_role == WorkspaceRole.OWNER and member.role != WorkspaceRole.OWNER:
        raise PermissionError("Owner access required.")

    return member


def get_user_workspaces(user_id: str):
    return WorkspaceMember.objects.filter(user_id=user_id).select_related("workspace")