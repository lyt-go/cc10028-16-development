import { createRouter, createWebHistory } from 'vue-router'

import LayoutRerunView from '@/views/LayoutRerunView.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/layout-rerun', name: 'layout-rerun', component: LayoutRerunView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
