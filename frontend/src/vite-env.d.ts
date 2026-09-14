/// <reference types="vite/client" />

/** 让 TS 认识 `import Foo from './Foo.vue'`。 */
declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<object, object, unknown>
  export default component
}
