"""Fixed baseline owner for the agent demo, eval, and tests."""

from pawpal_system import Owner, Pet


def build_fixture_owner() -> Owner:
    """Build a fresh owner with two task-free pets and a 120-minute budget."""
    # Fixed ids rather than uuids, so agent traces and fixtures stay reproducible.
    # No tasks, so a clean plan is reachable, unlike main.py's sample data.
    owner = Owner(owner_id="o-demo", name="Demo", available_time_minutes=120)
    owner.add_pet(Pet(pet_id="p-biscuit", name="Biscuit", species="dog"))
    owner.add_pet(Pet(pet_id="p-whiskers", name="Whiskers", species="cat"))
    return owner
