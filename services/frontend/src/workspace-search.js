import { inject, provide } from 'vue'

const workspaceSearch = Symbol('workspace search')

export function provideWorkspaceSearch (open) { provide(workspaceSearch, open) }
export function useWorkspaceSearch () { return inject(workspaceSearch) }
