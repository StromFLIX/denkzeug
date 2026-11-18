export function workflowSchedules (workflow) {
  if (workflow?.schedules) return workflow.schedules
  if (!workflow?.schedule) return []
  return [{ id: `schedule-${workflow.name}`, ...workflow.schedule }]
}

export function nextWorkflowSchedule (schedules) {
  return schedules.reduce((next, entry) => {
    if (entry.paused || !entry.next_run) return next
    return !next || Date.parse(entry.next_run) < Date.parse(next.next_run) ? entry : next
  }, null)
}
