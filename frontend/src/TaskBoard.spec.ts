import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import TaskBoard from './TaskBoard.vue'

describe('TaskBoard', () => {
  it('shows one task under primary and secondary, and unassigned alone', () => {
    const wrapper = mount(TaskBoard, {
      props: {
        items: [
          {
            id: '1',
            title: '完成代码评审',
            primary_owner: '张伟',
            secondary_owners: ['李娜'],
          },
          {
            id: '2',
            title: '跟进事项',
            primary_owner: '',
            secondary_owners: [],
            unresolved_ref: '她',
          },
        ],
      },
    })
    const text = wrapper.text()
    expect(text).toContain('张伟')
    expect(text).toContain('李娜')
    expect(wrapper.find('.task-card').element.tagName).toBe('BUTTON')
    expect(wrapper.findAll('.role-primary').length).toBe(1)
    expect(wrapper.findAll('.role-secondary').length).toBe(1)
    expect(text.match(/完成代码评审/g)?.length).toBe(2)
    expect(wrapper.text()).toContain('待分配')
    expect(wrapper.text()).toContain('跟进事项')
  })
})
