import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import Pipeline from './Pipeline.vue'

describe('Pipeline', () => {
  it('renders running, done, skipped, and waiting', () => {
    const wrapper = mount(Pipeline, {
      props: {
        statuses: {
          intake: 'done',
          clarify: 'skipped',
          decompose: 'running',
          assign_owners: 'waiting',
        },
      },
    })
    const item = (label: string) => wrapper.findAll('li').find((node) => node.text().includes(label))
    expect(item('解析')?.classes()).toContain('st-done')
    expect(item('澄清')?.classes()).toContain('st-skipped')
    expect(item('拆解')?.classes()).toContain('st-running')
    expect(item('分配')?.classes()).toContain('st-waiting')
    expect(item('分配')?.text()).toContain('待你操作')
  })
})
