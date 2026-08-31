#[repr(C)]
pub struct c_thing {
    pub value: i32,
}

pub unsafe fn c_touch(arg: *mut c_thing) -> *mut c_thing {
    unsafe {
        (*arg).value += 1;
    }
    arg
}

pub fn c_ping() {}

pub unsafe fn c_closure_touch(arg: *mut c_thing) -> *mut c_thing {
    unsafe {
        (*arg).value += 1;
    }
    arg
}
