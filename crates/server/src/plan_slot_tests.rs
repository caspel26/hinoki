use crate::CrudPlanSlot;

#[test]
fn crud_plan_slot_starts_disarmed_and_can_be_toggled() {
    let slot = CrudPlanSlot::new(9);
    assert_eq!(slot.action, 9);
    assert!(!slot.is_armed());
    slot.arm();
    assert!(slot.is_armed());
    slot.disarm();
    assert!(!slot.is_armed());
}
