import argparse
import sys
from pathlib import Path
from flows.pipeline_flow import LinkedInGrowthFlow
from flows.content_flow import LinkedInContentFlow

BASE_DIR = Path(__file__).resolve().parent
USERS_DIR = BASE_DIR / "artifacts" / "users"
DEFAULT_USER = "user_nikhil"


def get_available_users() -> list[str]:
    """Scans the artifacts/users directory for existing configured user accounts."""
    if not USERS_DIR.exists():
        return [DEFAULT_USER]
    users = [d.name for d in USERS_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")]
    return sorted(users) if users else [DEFAULT_USER]


def run_growth_pipeline(user_id: str):
    print("\n" + "=" * 70)
    print(f"   STARTING OUTBOUND GROWTH ENGINE [User: {user_id}]")
    print("=" * 70)
    growth_flow = LinkedInGrowthFlow(user_id=user_id)
    growth_flow.kickoff()

    print("\n" + "-" * 70)
    print(f"             GROWTH RUN SUMMARY [User: {user_id}]")
    print("-" * 70)
    print(f"Target Company    : {growth_flow.state.selected_company or 'None'}")
    print(f"Leads Harvested   : {growth_flow.state.harvested_count}")
    print(f"Stale Withdrawn   : {growth_flow.state.withdrawn_count}")
    print(f"Requests Sent     : {growth_flow.state.outreach_sent}")
    print("-" * 70)


def run_content_pipeline(user_id: str):
    print("\n" + "=" * 70)
    print(f"   STARTING INBOUND CONTENT ENGINE [User: {user_id}]")
    print("=" * 70)
    content_flow = LinkedInContentFlow(user_id=user_id)
    content_flow.kickoff()

    print("\n" + "-" * 70)
    print(f"            CONTENT RUN SUMMARY [User: {user_id}]")
    print("-" * 70)
    print(f"Content Type      : {content_flow.state.content_type.upper()}")
    print(f"Topic             : {content_flow.state.topic}")
    print(f"Approved by Human : {content_flow.state.approved_for_publishing}")
    publish_status = (
        content_flow.state.publish_result.get("status", "not_attempted")
        if content_flow.state.publish_result
        else "None"
    )
    print(f"Publish Status    : {publish_status}")
    print("-" * 70)


def execute_pipeline(user_id: str, mode: str):
    """Routes execution for a specific user based on the selected mode."""
    if mode == "growth":
        run_growth_pipeline(user_id)
    elif mode == "content":
        run_content_pipeline(user_id)
    elif mode == "full":
        run_growth_pipeline(user_id)
        run_content_pipeline(user_id)


def interactive_menu(active_user: str):
    """Interactive CLI terminal allowing flow selection and active user switching."""
    while True:
        available_users = get_available_users()
        print("\n" + "=" * 55)
        print("          LINKEDIN MASTER ORCHESTRATOR")
        print(f"          Active User: [{active_user}]")
        print("=" * 55)
        print("  [1] Run Outbound Growth Engine (Harvest, Clean, Connect)")
        print("  [2] Run Inbound Content Engine (Article/Post + HITL)")
        print("  [3] Run Full Sequence (Growth Engine -> Content Engine)")
        print("  [U] Switch Active User Profile")
        print("  [Q] Exit")
        print("-" * 55)

        choice = input("Select an option [1/2/3/U/Q]: ").strip().upper()

        if choice == "1":
            execute_pipeline(active_user, "growth")
        elif choice == "2":
            execute_pipeline(active_user, "content")
        elif choice == "3":
            execute_pipeline(active_user, "full")
        elif choice == "U":
            print("\nAvailable Users:")
            for idx, u in enumerate(available_users, 1):
                marker = " (Active)" if u == active_user else ""
                print(f"  [{idx}] {u}{marker}")
            print(f"  [N] Create new user profile")
            
            sel = input("\nChoose user index or [N]: ").strip().upper()
            if sel == "N":
                new_name = input("Enter new user ID (e.g. user_client_b): ").strip()
                if new_name:
                    active_user = new_name
            elif sel.isdigit() and 1 <= int(sel) <= len(available_users):
                active_user = available_users[int(sel) - 1]
        elif choice == "Q":
            print("\nExiting orchestrator. Goodbye!\n")
            sys.exit(0)
        else:
            print("\nInvalid choice. Please select 1, 2, 3, U, or Q.")


def main():
    parser = argparse.ArgumentParser(description="Multi-User LinkedIn Growth & Content Orchestrator")
    parser.add_argument(
        "--user",
        type=str,
        default=None,
        help="Target user_id (defaults to interactive prompt or 'user_nikhil')",
    )
    parser.add_argument(
        "--all-users",
        action="store_true",
        help="Run pipelines sequentially across all configured accounts in artifacts/users/",
    )
    parser.add_argument(
        "--mode",
        choices=["growth", "content", "full"],
        help="Execution mode (ideal for headless cron jobs and schedulers)",
    )

    args = parser.parse_args()

    # 1. Multi-user batch execution mode
    if args.all_users:
        users = get_available_users()
        target_mode = args.mode or "growth"
        print(f"\n[Batch Orchestrator] Running for all users: {users} in mode [{target_mode}]")
        for u in users:
            execute_pipeline(u, target_mode)
        return

    # 2. Single-user CLI execution via flags
    if args.user and args.mode:
        execute_pipeline(args.user, args.mode)
        return

    # 3. Interactive CLI menu (default)
    initial_user = args.user or DEFAULT_USER
    interactive_menu(initial_user)


if __name__ == "__main__":
    main()