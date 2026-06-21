// Demo exercising the precision: Box/ref derefs must NOT count as raw_ptr_derefs.
pub fn boxed() -> i32 {
    let b = Box::new(5);
    *b                       // safe Box deref -> NOT a raw ptr deref
}

pub unsafe fn raw(p: *const i32, q: *mut i32) -> i32 {
    let a = *p;              // raw deref 1 (no block; counted by type)
    *q = a;                  // raw deref 2 (assignment-target deref)
    unsafe {
        let r = *p.add(1);   // raw deref 3 (nested unsafe block)
        a + r
    }
}

pub fn mixed(p: *const i32) -> i32 {
    let bx = Box::new(1);
    let s = *bx;             // safe Box deref -> NOT raw
    unsafe { *p + s }        // raw deref 4 (unsafe block)
}
