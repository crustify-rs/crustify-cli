pub struct Holder {
    pub field: *mut fixture_sys::c_thing,
}

pub fn pass(
    arg: *mut fixture_sys::c_thing,
) -> *mut fixture_sys::c_thing {
    arg
}

pub fn read(arg: *mut fixture_sys::c_thing) -> i32 {
    unsafe { (*arg).value }
}

#[repr(transparent)]
pub struct Thing(fixture_sys::c_thing);

impl core::ops::Deref for Thing {
    type Target = fixture_sys::c_thing;

    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

impl core::ops::DerefMut for Thing {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.0
    }
}

pub fn shared_slice(_: &[Thing]) {}

pub fn mutable_slice(_: &mut [Thing]) {}

pub unsafe fn materialize_shared(ptr: *const Thing, len: usize) -> &'static [Thing] {
    unsafe { core::slice::from_raw_parts(ptr, len) }
}

pub unsafe fn materialize_mut(ptr: *mut Thing, len: usize) -> &'static mut [Thing] {
    unsafe { core::slice::from_raw_parts_mut(ptr, len) }
}

pub fn invoke_touch(
    arg: *mut fixture_sys::c_thing,
) -> *mut fixture_sys::c_thing {
    let local: *mut fixture_sys::c_thing = arg;
    unsafe { fixture_sys::c_touch(local) }
}

pub fn invoke_ping() {
    fixture_sys::c_ping();
}

pub fn invoke_closure_touch(
    arg: *mut fixture_sys::c_thing,
) -> Option<*mut fixture_sys::c_thing> {
    Some(arg).map(|local: *mut fixture_sys::c_thing| unsafe {
        fixture_sys::c_closure_touch(local)
    })
}

#[unsafe(export_name = "exported_touch")]
pub unsafe extern "C" fn rust_touch(
    arg: *mut fixture_sys::c_thing,
) -> *mut fixture_sys::c_thing {
    unsafe {
        (*arg).value += 1;
    }
    arg
}
