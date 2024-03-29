from torchvision import transforms
import numpy as np

def parse_transform(transform: str, image_size=224, **transform_kwargs):
    if transform == 'RandomColorJitter':
        return transforms.RandomApply([transforms.ColorJitter(0.4, 0.4, 0.4, 0.0)], p=1.0)
    elif transform == 'RandomGrayscale':
        return transforms.RandomGrayscale(p=0.1)
    elif transform == 'RandomGaussianBlur':
        return transforms.RandomApply([transforms.GaussianBlur(kernel_size=(5, 5))], p=0.3)
    elif transform == 'RandomCrop':
        return transforms.RandomCrop(image_size)
    elif transform == 'CenterCrop':
        return transforms.CenterCrop(image_size)
    elif transform == 'Resize_up':
        return transforms.Resize(
            [int(image_size * 1.15),
             int(image_size * 1.15)])
    elif transform == 'Normalize':
        return transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    elif transform == 'Resize':
        return transforms.Resize(
            [int(image_size),
             int(image_size)])
    elif transform == 'RandomRotation':
        return transforms.RandomRotation(degrees=10)
    elif transform == 'RandomResizedCrop':
        return transforms.RandomResizedCrop(image_size) 
    
        
    # Aug Intensity
    elif transform == 'RCrop_stronger':
        return transforms.RandomResizedCrop(image_size, scale = (0.01, 1.0))
    elif transform == 'RCrop_strong':
        return transforms.RandomResizedCrop(image_size, scale = (0.3, 1.0))
    elif transform == 'RCrop_weak':
        return transforms.RandomResizedCrop(image_size, scale = (0.6, 1.0))
    elif transform == 'RCrop_weaker':
        return transforms.RandomResizedCrop(image_size, scale = (0.9, 1.0))
    
    elif transform == 'CJitter_stronger':
        return transforms.RandomApply([transforms.ColorJitter((0.2, 1.8), (0.2, 1.8), (0.2, 1.8), (0,0))], p=1.0)
    elif transform == 'CJitter_strong':
        return transforms.RandomApply([transforms.ColorJitter((0.4, 1.6), (0.4, 1.6), (0.4, 1.6), (0,0))], p=1.0)
    elif transform == 'CJitter_weak':
        return transforms.RandomApply([transforms.ColorJitter((0.6, 1.4), (0.6, 1.4), (0.6, 1.4), (0,0))], p=1.0)
    elif transform == 'CJitter_weaker':
        return transforms.RandomApply([transforms.ColorJitter((0.8, 1.2), (0.8, 1.2), (0.8, 1.2), (0,0))], p=1.0)

    
    elif transform == '':
        return
    else:
        method = getattr(transforms, transform)
        return method(**transform_kwargs)

def get_single_transform(augmentation : str):
    transform = [augmentation]
    return transform + ['Resize', 'ToTensor', 'Normalize']

def get_fixed_transform_with_clean(aug_list, image_size=224) -> list: 
    transform_list = []
    # first, add original 
    original_transform = ['Resize', 'ToTensor', 'Normalize']
    transform_comp = transforms.Compose([parse_transform(x, image_size=image_size) for x in original_transform])
    transform_list.append(transform_comp)
    for aug in aug_list:
        transform_single = get_single_transform(aug)
        transform_comp = transforms.Compose([parse_transform(x, image_size=image_size) for x in transform_single])
        transform_list.append(transform_comp)
    return transform_list

def get_fixed_transform(aug_list, image_size=224) -> list:
    transform_list = []
    for aug in aug_list:
        transform_single = get_single_transform(aug)
        transform_comp = transforms.Compose([parse_transform(x, image_size=image_size) for x in transform_single])
        transform_list.append(transform_comp)
    return transform_list


def get_composed_transform(augmentation: str = None, image_size=224) -> transforms.Compose: 
    if augmentation == 'base':
        transform_list = ['RandomColorJitter', 'RandomResizedCrop', 'RandomHorizontalFlip', 'ToTensor',
                          'Normalize']
    elif augmentation == 'strong':
        transform_list = ['RandomResizedCrop', 'RandomColorJitter', 'RandomGrayscale', 'RandomGaussianBlur',
                          'RandomHorizontalFlip', 'ToTensor', 'Normalize']

    elif augmentation is None or augmentation.lower() == 'none':
        transform_list = ['Resize', 'ToTensor', 'Normalize'] 

    elif augmentation == 'rcrop':
        transform_list = ['RandomResizedCrop', 'ToTensor', 'Normalize']
    elif augmentation == 'cjitter':
        transform_list = ['RandomColorJitter', 'Resize', 'ToTensor', 'Normalize']
    elif augmentation == 'hflip':
        transform_list = ['RandomHorizontalFlip', 'Resize', 'ToTensor', 'Normalize']
    
    elif augmentation == 'base_weaker': 
        transform_list = ['CJitter_weaker', 'RCrop_weaker', 'RandomHorizontalFlip', 'ToTensor', 'Normalize']
    elif augmentation == 'base_weak': 
        transform_list = ['CJitter_weak', 'RCrop_weak', 'RandomHorizontalFlip', 'ToTensor', 'Normalize']
    elif augmentation == 'base_strong':
        transform_list = ['CJitter_strong', 'RCrop_strong', 'RandomHorizontalFlip', 'ToTensor', 'Normalize']
    elif augmentation == 'base_stronger': 
        transform_list = ['CJitter_stronger', 'RCrop_stronger', 'RandomHorizontalFlip', 'ToTensor', 'Normalize']

    else:
        raise ValueError('Unsupported augmentation: {}'.format(augmentation))

    transform_funcs = [parse_transform(x, image_size=image_size) for x in transform_list]
    transform = transforms.Compose(transform_funcs)
    return transform


# get 4 corner points of patches for CutMix
def rand_bbox(size, lam):
    W = size[2] 
    H = size[3] 

    cut_rat = np.sqrt(1. - lam)  
    cut_w = np.int(W * cut_rat) 
    cut_h = np.int(H * cut_rat)  
    
    cx = np.random.randint(W)
    cy = np.random.randint(H)

    bbx1 = np.clip(cx - cut_w // 2, 0, W)
    bby1 = np.clip(cy - cut_h // 2, 0, H)
    bbx2 = np.clip(cx + cut_w // 2, 0, W)
    bby2 = np.clip(cy + cut_h // 2, 0, H)

    return bbx1, bby1, bbx2, bby2