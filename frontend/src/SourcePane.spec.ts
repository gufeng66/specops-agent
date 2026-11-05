import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { describe, expect, it } from 'vitest'
import SourcePane from './SourcePane.vue'

const base = {
  text: '会议原文在这里',
  mode: 'read' as const,
  expanded: false,
  drawer: false,
  open: false,
  parts: null,
  hint: '',
  softLimit: 8000,
  hardLimit: 100000,
}

describe('SourcePane', () => {
  it('moves focus into the enlarged source and returns it when closed', async () => {
    const wrapper = mount(SourcePane, { props: base, attachTo: document.body })
    const trigger = wrapper.findAll('button').find((button) => button.text().includes('放大原文'))
    expect(trigger).toBeTruthy()
    trigger!.element.focus()
    await wrapper.setProps({ expanded: true })
    await nextTick()
    const dialog = document.querySelector('[role="dialog"]')
    expect(dialog?.contains(document.activeElement)).toBe(true)
    await wrapper.setProps({ expanded: false })
    await nextTick()
    expect(document.activeElement).toBe(trigger!.element)
    wrapper.unmount()
  })
})
